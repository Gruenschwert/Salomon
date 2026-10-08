"""Asana-Client (REST-API 1.0). Enthält selbst keine Tools."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Self

import httpx

from app.auth.tresor import Tresor, TresorFehler, lade_geheimnis
from app.config import Settings
from app.tools.base import ToolFehler, ToolKontext, aktueller_nutzer

log = logging.getLogger(__name__)

BASIS_URL = "https://app.asana.com/api/1.0"
TIMEOUT_SEKUNDEN = 10.0
MAX_WIEDERHOLUNGEN = 3
MAX_WARTEZEIT_SEKUNDEN = 60.0
SEITENGROESSE = 100
# Obergrenze für Einträge, die an Claude gehen.
MAX_EINTRAEGE = 30
MAX_FEHLERTEXT_ZEICHEN = 200
MASKIERT = "***"

DIENST = "asana"
NICHT_VERBUNDEN_TEXT = "Verbinde zuerst deinen Asana-Zugang mit /verbinden asana."
KEIN_SCHLUESSEL_TEXT = (
    "Persönliche Asana-Zugänge sind auf dem Server noch nicht eingerichtet "
    "(SECRETS_MASTER_KEY fehlt). Bitte sprich den Admin an."
)
NICHT_KONFIGURIERT_TEXT = NICHT_VERBUNDEN_TEXT
ZUGRIFF_VERWEIGERT_TEXT = "Asana-Zugriff verweigert."
# 402 und 403 kommen auch dann, wenn eine Funktion im Tarif fehlt (Felder, Zeiterfassung,
# Portfolios, Ziele, Startzeiten). Asana unterscheidet das nicht zuverlässig.
TARIF_TEXT = (
    "Asana-Zugriff verweigert: Das ist in eurem Asana-Tarif nicht verfügbar oder dein Token "
    "darf das nicht."
)
DOWNLOAD_TIMEOUT_SEKUNDEN = 30.0
UPLOAD_TIMEOUT_SEKUNDEN = 120.0
NICHT_ERREICHBAR_TEXT = "Asana ist gerade nicht erreichbar."
UNKLAR_TEXT = (
    "Asana hat nicht geantwortet. Ob die Änderung angekommen ist, ist unklar. "
    "Bitte in Asana prüfen; es wurde nichts wiederholt."
)
LIMIT_TEXT = "Das Asana-Abfragelimit ist erreicht. Bitte später erneut versuchen."

Schlaf = Callable[[float], Awaitable[None]]


class AsanaFehler(ToolFehler):
    """Fehler eines Asana-Aufrufs; `status` ist der HTTP-Status, falls es eine Antwort gab."""

    def __init__(self, meldung: str, status: int | None = None) -> None:
        super().__init__(meldung)
        self.status = status


def erster_admin(settings: Settings) -> int | None:
    """Die Telegram-ID, der der bisherige gemeinsame ASANA_TOKEN aus der .env gehört."""
    return min(settings.telegram_admin_user_ids, default=None)


async def lade_asana_token(kontext: ToolKontext, nutzer: object) -> str:
    """Der persönliche Asana-Token der Person. Es gibt keinen gemeinsamen Token als Rückfall.

    Einzige Ausnahme, damit die alte Funktion ohne Handgriff weiterläuft: Solange auf dem
    Server kein SECRETS_MASTER_KEY gesetzt ist, gehört der ASANA_TOKEN aus der .env weiter dem
    bisherigen Admin und nur ihm.
    """
    settings = kontext.settings
    try:
        tresor = Tresor.aus_settings(settings)
        if tresor.verfuegbar:
            token = await lade_geheimnis(kontext.session_fabrik, tresor, nutzer, DIENST)
            if token:
                return token
            raise AsanaFehler(NICHT_VERBUNDEN_TEXT)
    except TresorFehler as exc:
        raise AsanaFehler(f"Asana-Zugang nicht lesbar: {exc}") from None
    alter_token = settings.asana_token.get_secret_value().strip()
    if alter_token and getattr(nutzer, "telegram_id", None) == erster_admin(settings):
        return alter_token
    raise AsanaFehler(KEIN_SCHLUESSEL_TEXT)


class _Sitzung:
    """Offene Verbindung einer Person samt ihrem Token. Lebt nur während eines Tool-Aufrufs."""

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.http = http
        self.token = token
        self.tiefe = 0


class AsanaClient:
    """Lebt so lange wie das Tool. `async with` hält die Verbindung über mehrere Aufrufe offen.

    Jede Person hat ihre eigene Verbindung mit ihrem eigenen Token. Der Token wird erst beim
    Öffnen entschlüsselt und mit dem Schließen wieder verworfen.
    """

    def __init__(self, kontext: ToolKontext, schlaf: Schlaf = asyncio.sleep) -> None:
        self._kontext = kontext
        self._schlaf = schlaf
        # Nutzer-ID -> offene Verbindung. Zwei Personen teilen nie eine Verbindung.
        self._sitzungen: dict[int, _Sitzung] = {}
        self._workspace_gids: dict[int, str] = {}

    @staticmethod
    def _nutzer() -> object:
        nutzer = aktueller_nutzer.get(None)
        if nutzer is None:
            raise AsanaFehler(NICHT_VERBUNDEN_TEXT)
        return nutzer

    @property
    def _http(self) -> httpx.AsyncClient | None:
        nutzer = aktueller_nutzer.get(None)
        sitzung = self._sitzungen.get(nutzer.nutzer_id) if nutzer is not None else None
        return sitzung.http if sitzung else None

    async def __aenter__(self) -> Self:
        nutzer = self._nutzer()
        sitzung = self._sitzungen.get(nutzer.nutzer_id)
        if sitzung is None:
            token = await lade_asana_token(self._kontext, nutzer)
            sitzung = _Sitzung(self._neuer_http_client(token), token)
            self._sitzungen[nutzer.nutzer_id] = sitzung
        sitzung.tiefe += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        nutzer = self._nutzer()
        sitzung = self._sitzungen[nutzer.nutzer_id]
        sitzung.tiefe -= 1
        if sitzung.tiefe == 0:
            del self._sitzungen[nutzer.nutzer_id]
            await sitzung.http.aclose()

    async def get(self, pfad: str, params: dict | None = None, felder: Sequence[str] = ()) -> Any:
        return (await self._anfrage("GET", pfad, _mit_feldern(params, felder)))["data"]

    async def liste(
        self,
        pfad: str,
        params: dict | None = None,
        felder: Sequence[str] = (),
        max_eintraege: int = MAX_EINTRAEGE,
    ) -> tuple[list[dict], bool]:
        """Folgt der Paginierung bis `max_eintraege`. Zweiter Wert: Es gibt noch weitere."""
        eintraege: list[dict] = []
        params = _mit_feldern(params, felder)
        offset = None
        while True:
            seite = {**params, "limit": min(SEITENGROESSE, max_eintraege - len(eintraege) + 1)}
            if offset:
                seite["offset"] = offset
            inhalt = await self._anfrage("GET", pfad, seite)
            eintraege.extend(inhalt["data"])
            offset = (inhalt.get("next_page") or {}).get("offset")
            if len(eintraege) > max_eintraege:
                return eintraege[:max_eintraege], True
            if not offset:
                return eintraege, False

    async def post(self, pfad: str, daten: dict, felder: Sequence[str] = ()) -> Any:
        return (await self._anfrage("POST", pfad, _mit_feldern(None, felder), daten))["data"]

    async def put(self, pfad: str, daten: dict, felder: Sequence[str] = ()) -> Any:
        return (await self._anfrage("PUT", pfad, _mit_feldern(None, felder), daten))["data"]

    async def delete(self, pfad: str) -> None:
        await self._anfrage("DELETE", pfad, {})

    async def post_formular(
        self,
        pfad: str,
        felder: dict[str, str],
        datei: tuple[str, bytes, str] | None = None,
    ) -> Any:
        """POST als multipart/form-data (so verlangt es /attachments), ohne `data`-Hülle.

        `datei` ist (Dateiname, Inhalt, Medientyp) und geht als Teil `file` mit.
        """
        teile: dict = {name: (None, wert) for name, wert in felder.items()}
        if datei is not None:
            teile["file"] = datei
        return (await self._anfrage("POST", pfad, {}, formular=teile))["data"]

    async def roh(
        self, methode: str, pfad: str, params: dict | None = None, daten: dict | None = None
    ) -> dict:
        """Beliebiger Aufruf; liefert die ganze Antwort samt `next_page`. Der Aufrufer muss
        den Pfad vorher geprüft haben."""
        return await self._anfrage(methode, pfad, params or {}, daten)

    async def warte(self, sekunden: float) -> None:
        """Pause zwischen zwei Abfragen (in Tests ersetzt)."""
        await self._schlaf(sekunden)

    async def workspace_gid(self) -> str:
        """Der konfigurierte Workspace oder der einzige, den der Token sieht."""
        if konfiguriert := self._kontext.settings.asana_workspace_gid.strip():
            return konfiguriert
        nutzer_id = self._nutzer().nutzer_id
        if nutzer_id not in self._workspace_gids:
            workspaces, _ = await self.liste("/workspaces", felder=("name",), max_eintraege=20)
            if not workspaces:
                raise AsanaFehler("Der Asana-Token sieht keinen Workspace.")
            if len(workspaces) > 1:
                auswahl = ", ".join(f"{w.get('name', '')} ({w['gid']})" for w in workspaces)
                raise AsanaFehler(
                    "Der Asana-Token sieht mehrere Workspaces. Bitte einen in "
                    f"ASANA_WORKSPACE_GID eintragen: {auswahl}"
                )
            self._workspace_gids[nutzer_id] = workspaces[0]["gid"]
        return self._workspace_gids[nutzer_id]

    def _neuer_http_client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASIS_URL,
            timeout=TIMEOUT_SEKUNDEN,
            transport=self._kontext.http_transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _anfrage(
        self,
        methode: str,
        pfad: str,
        params: dict,
        daten: dict | None = None,
        formular: dict | None = None,
    ) -> dict:
        if self._http is None:
            async with self:
                return await self._anfrage(methode, pfad, params, daten, formular)
        schreibend = methode != "GET"
        if formular is not None:
            inhalt = {"files": formular, "timeout": UPLOAD_TIMEOUT_SEKUNDEN}
        else:
            inhalt = {"json": {"data": daten} if daten is not None else None}
        wiederholungen = 0
        while True:
            try:
                antwort = await self._http.request(methode, pfad, params=params, **inhalt)
            except httpx.HTTPError as exc:
                # Nur der Typ: Meldungstexte der Netzwerkschicht gehören nicht ins Log.
                log.warning("Asana nicht erreichbar (%s %s): %s", methode, pfad, type(exc).__name__)
                # Schreibende Aufrufe nie wiederholen, sonst drohen Duplikate.
                if schreibend:
                    raise AsanaFehler(UNKLAR_TEXT) from None
                if wiederholungen >= MAX_WIEDERHOLUNGEN:
                    raise AsanaFehler(NICHT_ERREICHBAR_TEXT) from None
                wiederholungen += 1
                continue
            if antwort.status_code == 429:
                # Bei 429 hat Asana nichts ausgeführt, deshalb ist die Wiederholung auch
                # für schreibende Aufrufe sicher.
                if wiederholungen >= MAX_WIEDERHOLUNGEN:
                    raise AsanaFehler(LIMIT_TEXT, status=429)
                wiederholungen += 1
                await self._schlaf(_wartezeit(antwort))
                continue
            return self._auswerten(antwort)

    def _auswerten(self, antwort: httpx.Response) -> dict:
        status = antwort.status_code
        if status == 401:
            raise AsanaFehler(ZUGRIFF_VERWEIGERT_TEXT, status=status)
        if status in (402, 403):
            raise AsanaFehler(TARIF_TEXT, status=status)
        if status == 404:
            raise AsanaFehler("Asana kennt dieses Objekt nicht (GID prüfen).", status=status)
        if status >= 500:
            raise AsanaFehler(f"Asana antwortet mit Status {status}.", status=status)
        try:
            inhalt = antwort.json()
        except ValueError:
            inhalt = None
        if status >= 400:
            raise AsanaFehler(
                f"Asana lehnt die Anfrage ab: {self._fehlertext(inhalt)}", status=status
            )
        if not isinstance(inhalt, dict) or "data" not in inhalt:
            raise AsanaFehler("Asana hat eine unerwartete Antwort geliefert.", status=status)
        return inhalt

    def _fehlertext(self, inhalt: object) -> str:
        fehler = inhalt.get("errors") if isinstance(inhalt, dict) else None
        meldungen = [str(f.get("message", "")) for f in fehler or [] if isinstance(f, dict)]
        text = "; ".join(m for m in meldungen if m) or "ohne Begründung"
        sitzung = self._sitzungen.get(self._nutzer().nutzer_id)
        if sitzung is not None:
            text = text.replace(sitzung.token, MASKIERT)
        return text[:MAX_FEHLERTEXT_ZEICHEN]


def pruefe_gid(wert: object, feld: str = "gid") -> str:
    """GIDs landen im URL-Pfad und müssen deshalb rein numerisch sein."""
    text = str(wert).strip() if isinstance(wert, str | int) else ""
    if not text.isascii() or not text.isdigit():
        raise ToolFehler(
            f"„{feld}“ ist keine gültige Asana-GID. GIDs stammen immer aus einem Lese-Tool."
        )
    return text


def _mit_feldern(params: dict | None, felder: Sequence[str]) -> dict:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if felder:
        params["opt_fields"] = ",".join(felder)
    return params


def _wartezeit(antwort: httpx.Response) -> float:
    try:
        sekunden = float(antwort.headers.get("Retry-After", "1"))
    except ValueError:
        sekunden = 1.0
    return max(0.0, min(sekunden, MAX_WARTEZEIT_SEKUNDEN))


def begrenze(eintraege: list, weitere: bool = False, maximum: int = MAX_EINTRAEGE) -> dict:
    """Kürzt eine Trefferliste für Claude und sagt dazu, wenn etwas fehlt."""
    ergebnis: dict = {"eintraege": eintraege[:maximum], "anzahl": min(len(eintraege), maximum)}
    if weitere or len(eintraege) > maximum:
        ergebnis["hinweis"] = (
            f"Es gibt mehr Treffer; angezeigt werden {ergebnis['anzahl']}. Suche enger fassen."
        )
    return ergebnis


def kuerze_text(text: str | None, maximum: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


async def lade_herunter(kontext: ToolKontext, url: str, max_bytes: int) -> bytes:
    """Lädt eine Datei von einer Download-Adresse, die Asana geliefert hat.

    Bewusst ohne den Asana-Client: Die Adresse zeigt auf einen Dateispeicher außerhalb der
    API, dorthin darf der Token nie mitgeschickt werden.
    """
    if not url.startswith("https://"):
        raise AsanaFehler("Asana hat keine gültige Download-Adresse geliefert.")
    daten = bytearray()
    try:
        async with (
            httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT_SEKUNDEN,
                transport=kontext.http_transport,
                follow_redirects=True,
            ) as client,
            client.stream("GET", url) as antwort,
        ):
            if antwort.status_code != 200:
                raise AsanaFehler(f"Der Download antwortet mit Status {antwort.status_code}.")
            async for stueck in antwort.aiter_bytes():
                daten.extend(stueck)
                if len(daten) > max_bytes:
                    raise AsanaFehler("Die Datei ist größer als erlaubt.")
    except httpx.HTTPError as exc:
        log.warning("Download nicht möglich: %s", type(exc).__name__)
        raise AsanaFehler("Die Datei konnte nicht heruntergeladen werden.") from None
    return bytes(daten)


async def pruefe_token(kontext: ToolKontext, token: str) -> str:
    """Testet einen Asana-Token mit GET /users/me und liefert den Namen des Kontos.

    Der Token steht dabei nur im Authorization-Header dieser einen Anfrage.
    """
    try:
        async with httpx.AsyncClient(
            base_url=BASIS_URL,
            timeout=TIMEOUT_SEKUNDEN,
            transport=kontext.http_transport,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            antwort = await client.get("/users/me", params={"opt_fields": "name"})
    except httpx.HTTPError as exc:
        log.warning("Asana-Token konnte nicht geprüft werden: %s", type(exc).__name__)
        raise AsanaFehler(NICHT_ERREICHBAR_TEXT) from None
    if antwort.status_code in (401, 403):
        raise AsanaFehler("Asana lehnt diesen Token ab.", status=antwort.status_code)
    if antwort.status_code != 200:
        raise AsanaFehler(
            f"Asana antwortet mit Status {antwort.status_code}.", status=antwort.status_code
        )
    return str((antwort.json().get("data") or {}).get("name") or "")
