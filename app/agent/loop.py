"""Agent-Schleife."""

import base64
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from app.agent.history import lade_verlauf, speichere_austausch
from app.agent.prompts import baue_system_prompt
from app.auth.approvals import Freigaben
from app.auth.kontext import NutzerKontext
from app.channels.base import Antwort, EingehendeNachricht, FreigabeAnfrage
from app.config import Settings
from app.db.session import SessionFabrik
from app.medien import groesse_text
from app.observability.audit import protokolliere
from app.observability.costs import Kosten
from app.tools.base import ToolFehler, ToolKontext
from app.tools.registry import Registry, ToolErgebnis, fuehre_tool_aus

log = logging.getLogger(__name__)

MAX_ZWISCHENSTAND_ZEICHEN = 1500
# So oft wird eine am Ausgabelimit abgeschnittene Textantwort fortgesetzt.
MAX_FORTSETZUNGEN = 2
FORTSETZEN_TEXT = (
    "[System] Deine Antwort wurde am Ausgabelimit abgeschnitten. Fahre genau dort fort, wo du "
    "aufgehört hast, ohne etwas zu wiederholen. Beginne mit einer neuen Zeile."
)
TOOL_ABGESCHNITTEN_TEXT = (
    "[System] Dein Tool-Aufruf war zu lang und wurde am Ausgabelimit abgeschnitten. Er wurde "
    "NICHT ausgeführt, es gibt keine Freigabe. Versuche es jetzt kürzer: Nutze bei "
    "gleichartigen Operationen die Sammelform „gids“, lass Erklärtext weg und teile sehr "
    "große Sätze in Pakete."
)
TOOL_ZU_LANG_TEXT = (
    "Mein Tool-Aufruf war zu lang und wurde am Ausgabelimit (MAX_OUTPUT_TOKENS={limit}) "
    "abgeschnitten, auch im zweiten, kürzeren Versuch. Es wurde nichts vorbereitet und nichts "
    "geändert. Bitte grenze die Aufgabe ein oder lass MAX_OUTPUT_TOKENS erhöhen."
)
NUR_FREIGABE_TEXT = "Ich habe den Änderungssatz vorbereitet. Die Vorschau mit den Buttons folgt."
WARTET_AUF_FREIGABE_TEXT = (
    "Wartet auf Freigabe des Nutzers. Das Tool wurde NICHT ausgeführt. Der Nutzer sieht jetzt "
    "eine Vorschau mit den Buttons ✅ / ❌. Beende die Runde mit einem kurzen Hinweis darauf."
)
TAGESLIMIT_TEXT = (
    "Das Tageslimit für KI-Kosten ist erreicht. Neue Anfragen sind ab morgen wieder möglich."
)
DIENST_FEHLER_TEXT = "Der KI-Dienst ist gerade nicht erreichbar. Bitte versuche es später erneut."
ABLEHNUNG_TEXT = "Diese Anfrage kann ich nicht beantworten."
LEERE_ANTWORT_TEXT = "Dazu habe ich keine Antwort erhalten. Bitte formuliere die Frage neu."
GEKUERZT_HINWEIS = (
    "\n\n(Hier endet meine Antwort am Ausgabelimit. Schreib „weiter“, dann setze ich fort.)"
)
FOTO_MARKE = "[Foto]"
FOTO_OHNE_TEXT = "Bitte lies dieses Foto."
DATEI_OHNE_TEXT = "(Der Nutzer hat nichts dazu geschrieben.)"
DATEIEN_KOPF = (
    "[Dateien dieser Nachricht. Zum Anhängen an Asana den Verweis in anhang_hinzufuegen als "
    "„datei“ angeben:]"
)


class Agent:
    def __init__(
        self,
        settings: Settings,
        session_fabrik: SessionFabrik,
        client: anthropic.AsyncAnthropic,
        registry: Registry,
        freigaben: Freigaben,
        kosten: Kosten,
    ) -> None:
        self._settings = settings
        self._session_fabrik = session_fabrik
        self._client = client
        self._registry = registry
        self._freigaben = freigaben
        self._kosten = kosten
        self._kontext = ToolKontext(settings=settings, session_fabrik=session_fabrik)

    async def beantworte(self, nachricht: EingehendeNachricht, user: NutzerKontext) -> Antwort:
        if await self._kosten.limit_erreicht():
            return Antwort(text=TAGESLIMIT_TEXT)
        verlauf = await lade_verlauf(
            self._session_fabrik, user, nachricht.chat_id, self._settings.history_max_messages
        )
        messages = [*verlauf, {"role": "user", "content": _inhalt(nachricht)}]
        # Freigaben, die in dieser Runde angelegt wurden; der Kanal zeigt sie mit Buttons an.
        anfragen: list[FreigabeAnfrage] = []
        try:
            text = await self._schleife(messages, user, anfragen, await self._freigaben.stand(user))
        except anthropic.APIError:
            log.exception("Claude-Aufruf fehlgeschlagen")
            return Antwort(text=DIENST_FEHLER_TEXT, freigaben=tuple(anfragen))
        await speichere_austausch(
            self._session_fabrik, nachricht.chat_id, user.id, _verlaufstext(nachricht), text
        )
        return Antwort(text=text, freigaben=tuple(anfragen))

    async def _schleife(
        self,
        messages: list[dict],
        user: NutzerKontext,
        anfragen: list[FreigabeAnfrage],
        freigaben_stand: str = "",
    ) -> str:
        settings = self._settings
        anfrage = {
            "model": settings.model_default,
            "max_tokens": settings.max_output_tokens,
            "system": baue_system_prompt(datetime.now(ZoneInfo(settings.tz)), freigaben_stand),
        }
        if tools := self._registry.api_definitionen(user.rolle):
            anfrage["tools"] = tools
        tool_aufrufe = 0
        letztes_tool = ""
        zwischenstand = ""
        teile: list[str] = []
        tool_abgeschnitten = False
        for runde in range(1, settings.agent_max_rounds + 1):
            response = await self._client.messages.create(**anfrage, messages=messages)
            await self._kosten.verbuche(user.id, *_tokens(response.usage))
            text = _text(response)
            if response.stop_reason == "tool_use":
                tool_abgeschnitten = False
                zwischenstand = text or zwischenstand
                messages.append({"role": "assistant", "content": response.content})
                ergebnisse = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    tool_aufrufe += 1
                    letztes_tool = block.name
                    ergebnis = await self._bearbeite_tool_anfrage(
                        block.name, block.input, user, anfragen
                    )
                    ergebnisse.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": [{"type": "text", "text": ergebnis.text}, *ergebnis.bloecke]
                            if ergebnis.bloecke
                            else ergebnis.text,
                            "is_error": ergebnis.fehler,
                        }
                    )
                messages.append({"role": "user", "content": ergebnisse})
                continue
            if response.stop_reason == "refusal":
                return ABLEHNUNG_TEXT
            if response.stop_reason == "max_tokens":
                if any(block.type == "tool_use" for block in response.content):
                    # Der Tool-Aufruf ist unvollständig und darf weder ausgeführt noch in den
                    # Verlauf übernommen werden. Claude bekommt einen zweiten, kürzeren Versuch.
                    log.warning(
                        "Tool-Aufruf am Ausgabelimit abgeschnitten (Runde %s, max_tokens=%s)",
                        runde,
                        settings.max_output_tokens,
                    )
                    if tool_abgeschnitten:
                        return TOOL_ZU_LANG_TEXT.format(limit=settings.max_output_tokens)
                    tool_abgeschnitten = True
                    messages.append(
                        {"role": "assistant", "content": text or "(Tool-Aufruf begonnen)"}
                    )
                    messages.append({"role": "user", "content": TOOL_ABGESCHNITTEN_TEXT})
                    continue
                if text and len(teile) < MAX_FORTSETZUNGEN:
                    teile.append(text)
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": FORTSETZEN_TEXT})
                    continue
            gesamt = "\n".join([*teile, text]).strip()
            if not gesamt:
                if anfragen:
                    return NUR_FREIGABE_TEXT
                # Wirklich kein Text: festhalten, wie es dazu kam.
                log.warning(
                    "Leere Antwort von Claude: stop_reason=%s, Runden=%s, Tool-Aufrufe=%s, "
                    "letztes Tool=%s",
                    response.stop_reason,
                    runde,
                    tool_aufrufe,
                    letztes_tool or "-",
                )
                return LEERE_ANTWORT_TEXT
            if response.stop_reason == "max_tokens":
                gesamt += GEKUERZT_HINWEIS
            return gesamt
        log.warning(
            "Rundenlimit erreicht: Runden=%s, Tool-Aufrufe=%s, letztes Tool=%s",
            settings.agent_max_rounds,
            tool_aufrufe,
            letztes_tool or "-",
        )
        return _rundenlimit_text(
            settings.agent_max_rounds, tool_aufrufe, letztes_tool, zwischenstand, len(anfragen)
        )

    async def _bearbeite_tool_anfrage(
        self, name: str, params: dict, user: NutzerKontext, anfragen: list[FreigabeAnfrage]
    ) -> ToolErgebnis:
        tool = self._registry.hole(name)
        if tool is None or user.rolle not in tool.erlaubte_rollen:
            await protokolliere(
                self._session_fabrik,
                user_id=user.id,
                tool_name=name,
                parameter=params,
                fehler="nicht verfügbar",
            )
            return ToolErgebnis(f"Das Tool {name} ist nicht verfügbar.", fehler=True)
        if not tool.ist_schreibend(params):
            return await fuehre_tool_aus(tool, params, user, self._kontext)
        # Schreibend: NICHT ausführen, sondern Freigabe anlegen.
        try:
            anfragen.append(await self._freigaben.anfragen(user, tool, params))
        except ToolFehler as exc:
            return ToolErgebnis(f"Fehler: {exc}", fehler=True)
        except Exception:
            log.exception("Freigabe für Tool %s konnte nicht angelegt werden", name)
            return ToolErgebnis(f"Interner Fehler im Tool {name}.", fehler=True)
        return ToolErgebnis(WARTET_AUF_FREIGABE_TEXT)


def _inhalt(nachricht: EingehendeNachricht) -> str | list[dict]:
    """Inhalt der Nutzer-Nachricht für Claude; Fotos gehen als base64-Bildblöcke mit."""
    text = _text_mit_dateien(nachricht, FOTO_OHNE_TEXT if nachricht.bilder else DATEI_OHNE_TEXT)
    if not nachricht.bilder:
        return text
    bloecke: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": bild.medientyp,
                "data": base64.standard_b64encode(bild.daten).decode("ascii"),
            },
        }
        for bild in nachricht.bilder
    ]
    bloecke.append({"type": "text", "text": text})
    return bloecke


def _text_mit_dateien(nachricht: EingehendeNachricht, ohne_text: str) -> str:
    """Hängt die Verweise auf mitgeschickte Dateien an den Text der Nachricht."""
    if not nachricht.dateien:
        return nachricht.text or (ohne_text if nachricht.bilder else "")
    zeilen = [nachricht.text or ohne_text, "", DATEIEN_KOPF]
    zeilen += [
        f"- {d.verweis}: „{d.name}“ ({d.medientyp or 'unbekannter Typ'}, {groesse_text(d.groesse)})"
        for d in nachricht.dateien
    ]
    return "\n".join(zeilen)


def _verlaufstext(nachricht: EingehendeNachricht) -> str:
    """Im Verlauf steht statt des Bildes nur „[Foto]“ plus Bildunterschrift."""
    # Die Verweise bleiben im Verlauf, damit „häng das an Aufgabe X“ auch nach einer Rückfrage
    # noch funktioniert. Gespeichert wird nur der Verweis, nie der Inhalt.
    text = _text_mit_dateien(nachricht, "")
    if not nachricht.bilder:
        return text.strip()
    marken = " ".join([FOTO_MARKE] * len(nachricht.bilder))
    return f"{marken} {text}".strip()


def _tokens(usage: anthropic.types.Usage) -> tuple[int, int]:
    """Input- und Output-Tokens; Cache-Tokens zählen vorsichtshalber voll als Input."""
    eingabe = (
        usage.input_tokens
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    )
    return eingabe, usage.output_tokens


def _text(response: anthropic.types.Message) -> str:
    return "\n".join(block.text for block in response.content if block.type == "text").strip()


def _rundenlimit_text(
    runden: int, tool_aufrufe: int, letztes_tool: str, zwischenstand: str, freigaben: int
) -> str:
    """Ehrliche Meldung mit Zwischenstand, wenn die Aufgabe im Rundenlimit nicht fertig wird."""
    zeilen = [
        f"Ich habe nach {runden} Runden aufgehört und bin mit der Aufgabe nicht fertig geworden "
        f"({tool_aufrufe} Tool-Aufrufe, zuletzt {letztes_tool or 'keiner'})."
    ]
    if zwischenstand:
        if len(zwischenstand) > MAX_ZWISCHENSTAND_ZEICHEN:
            zwischenstand = zwischenstand[: MAX_ZWISCHENSTAND_ZEICHEN - 1] + "…"
        zeilen.append(f"Zwischenstand: {zwischenstand}")
    if freigaben == 1:
        zeilen.append("Ein Änderungssatz ist vorbereitet und wartet auf deine Freigabe.")
    elif freigaben:
        zeilen.append(f"{freigaben} Änderungssätze sind vorbereitet und warten auf deine Freigabe.")
    else:
        zeilen.append("Es wurde nichts geändert.")
    zeilen.append(
        "Schreib „weiter“, dann mache ich an dieser Stelle weiter, oder grenze die Aufgabe ein."
    )
    return "\n".join(zeilen)
