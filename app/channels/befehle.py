"""Slash-Befehle. Sie laufen direkt auf dem Server und nie über das Modell."""

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.agent.gedaechtnis import GedaechtnisFehler, lade_notizen, merke, vergiss_alles
from app.agent.router import AUTO, STUFEN, ModellVorgaben
from app.auth.kontext import NutzerKontext
from app.auth.rechte import ADMIN_KOSTEN_ALLE, ADMIN_NUTZER, MAIL_EIGENE, RECHTE
from app.auth.users import (
    NutzerFehler,
    aendere_profil,
    aendere_rolle,
    finde_nutzer,
    lege_nutzer_an,
    liste_nutzer,
    setze_sperre,
)
from app.auth.zugaenge import DIENSTE, MAIL, Zugaenge
from app.db.models import ROLLEN
from app.db.session import SessionFabrik
from app.mail.konten import ist_adresse, lies_server, pruefe_label
from app.observability.audit import protokolliere
from app.observability.costs import ERLAUBTE_ZEITRAEUME, Kosten, als_euro
from app.tools.base import ToolFehler

# So lange wartet der Bot nach /verbinden auf die Nachricht mit dem Token.
GEHEIMNIS_FRIST_SEKUNDEN = 600
# So lange gilt der Bestätigungsbutton einer Admin-Aktion.
AKTION_FRIST_SEKUNDEN = 300
GRENZE_TEXT = (
    "Zu deinen Daten: Dein Verlauf, deine Notizen und deine Zugänge gehören nur dir. Kein "
    "anderer Nutzer und kein Admin kann sie über den Bot lesen; Zugangsdaten liegen "
    "verschlüsselt in der Datenbank. Ehrliche Grenze: Wer Root-Zugriff auf den Server hat und "
    "den Hauptschlüssel kennt, könnte technisch entschlüsseln."
)
NUR_PRIVAT_TEXT = (
    "Zugangsdaten nehme ich nur im privaten Chat mit mir an, nie in einer Gruppe. Schreib mir "
    "dort /verbinden."
)


MAIL_KI_HINWEIS = (
    "Wichtig vorab: Wenn du mich nach deinen Mails fragst, gehen die Inhalte dieser Mails zur "
    "Verarbeitung an den KI-Dienst Anthropic. Dauerhaft gespeichert werden sie bei mir nicht; "
    "im Gesprächsverlauf liegen sie verschlüsselt und werden nach {stunden} Stunden entfernt."
)
MAIL_KEIN_RECHT_TEXT = "Für Mails fehlt dir das Recht. Die Rolle dafür kann ein Admin vergeben."
NAME_BEHALTEN = {"ok", "okay", "nein", "nö", "passt", "ja", "gut", "danke"}
SIGNATUR_LOESCHEN = {"löschen", "loeschen", "keine", "-"}

# Arten von Eingaben, auf die der Bot nach einem Befehl wartet
ART_TOKEN = "token"
ART_MAIL_ADRESSE = "mail_adresse"
ART_MAIL_PASSWORT = "mail_passwort"
ART_MAIL_NAME = "mail_name"
ART_SIGNATUR = "signatur"


@dataclass
class _Dialog:
    """Die nächste Nachricht dieser Person in diesem Chat gehört zu einem Befehl und geht
    nicht an das Modell. `geheim`: Sie enthält Zugangsdaten und wird aus dem Chat gelöscht."""

    art: str
    gueltig_bis: float
    geheim: bool = False
    daten: dict = field(default_factory=dict, repr=False)

    @property
    def wort(self) -> str:
        return "Passwort" if self.art == ART_MAIL_PASSWORT else "Token"


@dataclass(frozen=True)
class BefehlsAntwort:
    text: str
    # Kennung einer Aktion, die der Admin noch per Button bestätigen muss
    aktion: str | None = None


@dataclass
class _Aktion:
    """Eine vorbereitete Admin-Aktion, die auf die Bestätigung per Button wartet."""

    telegram_id: int
    befehl: str
    beschreibung: str
    parameter: dict
    ausfuehren: Callable[[], Awaitable[None]]
    gueltig_bis: float
    # Recht, das beim Klick noch einmal geprüft wird; None = eigene Daten, kein Recht nötig
    recht: str | None = ADMIN_NUTZER


@dataclass(frozen=True)
class Befehl:
    name: str
    beschreibung: str
    funktion: Callable[..., Awaitable[str]]
    # Erforderliches Recht; None = für alle
    recht: str | None = None


class Befehle:
    """Alle Slash-Befehle mit ihren Abhängigkeiten. Der Kanal ruft `fuehre_aus` auf."""

    def __init__(
        self,
        session_fabrik: SessionFabrik,
        zugaenge: Zugaenge | None = None,
        kosten: Kosten | None = None,
        vorgaben: ModellVorgaben | None = None,
    ) -> None:
        self._session_fabrik = session_fabrik
        self._zugaenge = zugaenge
        self._kosten = kosten
        self._vorgaben = vorgaben or ModellVorgaben()
        # (Chat-ID, Telegram-ID) -> erwartete Eingabe. Die nächste Nachricht dieser Person in
        # diesem Chat gehört dann zum Befehl und geht an niemanden sonst.
        self._erwartet: dict[tuple[int, int], _Dialog] = {}
        self._aktionen: dict[str, _Aktion] = {}
        self._tabelle: dict[str, Befehl] = {}
        self.registriere(
            Befehl("start", "Begrüßung", self._start),
            Befehl("hilfe", "zeigt die Befehle, die du nutzen kannst", self._hilfe),
            Befehl(
                "verbinden",
                "eigenen Zugang verbinden: /verbinden asana oder /verbinden mail",
                self._verbinden,
            ),
            Befehl(
                "trennen",
                "eigenen Zugang entfernen: /trennen asana oder /trennen mail <name>",
                self._trennen,
            ),
            Befehl("verbunden", "zeigt, welche Dienste du verbunden hast", self._verbunden),
            Befehl(
                "signatur",
                "Signatur eines Postfachs ansehen oder ändern: /signatur <name>",
                self._signatur,
            ),
            Befehl("kosten", "deine Kosten: /kosten oder /kosten 7 (1, 3, 7, 30)", self._kosten_),
            Befehl("modell", "Modellstufe: /modell einfach, standard, komplex, auto", self._modell),
            Befehl("profil", "Name, Anrede und Zeitzone anzeigen oder ändern", self._profil),
            Befehl("merken", "persönliche Notiz speichern: /merken <text>", self._merken),
            Befehl("gemerkt", "zeigt deine Notizen", self._gemerkt),
            Befehl("vergessen", "löscht deinen Verlauf und deine Notizen", self._vergessen),
            Befehl("nutzer", "Liste aller Personen mit Rollen", self._nutzer, ADMIN_NUTZER),
            Befehl(
                "nutzer_neu",
                "Person anlegen: /nutzer_neu <telegram_id> <name>",
                self._nutzer_neu,
                ADMIN_NUTZER,
            ),
            Befehl(
                "rolle",
                "Rolle geben oder nehmen: /rolle <name> +buchhaltung",
                self._rolle,
                ADMIN_NUTZER,
            ),
            Befehl("sperren", "Person sperren: /sperren <name>", self._sperren, ADMIN_NUTZER),
            Befehl(
                "entsperren", "Sperre aufheben: /entsperren <name>", self._entsperren, ADMIN_NUTZER
            ),
            Befehl(
                "limit",
                "Tageslimit einer Person: /limit <name> <euro> (oder standard)",
                self._limit,
                ADMIN_NUTZER,
            ),
        )

    def registriere(self, *befehle: Befehl) -> None:
        for befehl in befehle:
            self._tabelle[befehl.name] = befehl

    def namen(self) -> list[str]:
        return list(self._tabelle)

    def erlaubte(self, nutzer: NutzerKontext) -> list[Befehl]:
        return [b for b in self._tabelle.values() if b.recht is None or nutzer.darf(b.recht)]

    async def fuehre_aus(
        self, name: str, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> BefehlsAntwort:
        ergebnis = await self._fuehre_aus(name, nutzer, argumente, chat_id, privat)
        return ergebnis if isinstance(ergebnis, BefehlsAntwort) else BefehlsAntwort(ergebnis)

    async def _fuehre_aus(
        self, name: str, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> "str | BefehlsAntwort":
        befehl = self._tabelle.get(name)
        if befehl is None:
            return "Diesen Befehl kenne ich nicht. /hilfe zeigt, was geht."
        if befehl.recht is not None and not nutzer.darf(befehl.recht):
            await protokolliere(
                self._session_fabrik,
                user_id=nutzer.id,
                tool_name=f"/{name}",
                parameter={},
                fehler="kein Recht",
            )
            return "Dafür fehlt dir das Recht. Die Rolle dafür kann ein Admin vergeben."
        try:
            return await befehl.funktion(nutzer, argumente, chat_id, privat)
        except (ToolFehler, NutzerFehler, GedaechtnisFehler) as exc:
            return str(exc)

    # ---------------------------------------------------------------- Kosten

    async def _kosten_(self, nutzer: NutzerKontext, argumente: list[str], *_: object) -> str:
        if self._kosten is None:
            return "Die Kostenauswertung ist hier nicht eingerichtet."
        alle = bool(argumente) and argumente[-1].lower() == "alle"
        zahlen = [a for a in argumente if a.lower() != "alle"]
        try:
            tage = int(zahlen[0]) if zahlen else 1
        except ValueError:
            tage = 0
        if tage not in ERLAUBTE_ZEITRAEUME or len(zahlen) > 1:
            return "So geht es: /kosten, /kosten 3, /kosten 7 oder /kosten 30"
        if alle:
            if not nutzer.darf(ADMIN_KOSTEN_ALLE):
                return "Die Kosten aller sieht nur, wer das Recht dafür hat. Deine eigenen: /kosten"
            return await self._kosten_alle(tage)
        return await self._kosten_eigene(nutzer, tage)

    @staticmethod
    def _zeitraum_text(tage: int, von, bis) -> str:
        if tage == 1:
            return f"heute ({bis:%d.%m.})"
        return f"letzte {tage} Tage ({von:%d.%m.} bis {bis:%d.%m.})"

    async def _kosten_eigene(self, nutzer: NutzerKontext, tage: int) -> str:
        auswertung = await self._kosten.auswertung(nutzer.id, tage)
        zeilen = [
            f"Deine Kosten, {self._zeitraum_text(tage, auswertung.von, auswertung.bis)}:",
            f"Gesamt: {als_euro(auswertung.summe_eur)} bei {auswertung.anfragen} "
            f"Anfrage{'' if auswertung.anfragen == 1 else 'n'} an die KI",
        ]
        if auswertung.je_modell:
            zeilen.append("Nach Modell:")
            zeilen += [
                f"- {modell}: {als_euro(summe)} ({anzahl})"
                for modell, (summe, anzahl) in auswertung.je_modell.items()
            ]
        if auswertung.je_grund:
            zeilen.append("Modellwahl:")
            zeilen += [f"- {grund}: {anzahl}" for grund, anzahl in auswertung.je_grund.items()]
        if auswertung.groesster_tag and tage > 1:
            tag, summe = auswertung.groesster_tag
            zeilen.append(f"Größter Tag: {tag:%d.%m.} mit {als_euro(summe)}")
        limit = await self._kosten.limit_von(nutzer.id)
        zeilen.append(
            f"Heute: {als_euro(await self._kosten.heute_eur(nutzer.id))} von {als_euro(limit)} "
            "Tageslimit"
        )
        return "\n".join(zeilen)

    async def _kosten_alle(self, tage: int) -> str:
        """Kosten je Person für Admins: nur Namen und Zahlen, nie Inhalte."""
        personen = await self._kosten.auswertung_alle(tage)
        von, bis = self._kosten._zeitraum(tage)
        zeilen = [f"Kosten aller, {self._zeitraum_text(tage, von, bis)}:"]
        zeilen += [f"- {name}: {als_euro(summe)} ({anzahl})" for name, summe, anzahl in personen]
        if not personen:
            zeilen.append("Keine Anfragen in diesem Zeitraum.")
        summe = sum((betrag for _, betrag, _ in personen), start=Decimal(0))
        anfragen = sum(anzahl for _, _, anzahl in personen)
        zeilen.append(f"Summe: {als_euro(summe)} bei {anfragen} Anfragen an die KI")
        return "\n".join(zeilen)

    async def _limit(
        self, nutzer: NutzerKontext, argumente: list[str], *_: object
    ) -> "str | BefehlsAntwort":
        if self._kosten is None:
            return "Limits sind hier nicht eingerichtet."
        if len(argumente) < 2:
            return "So geht es: /limit <name> <euro> oder /limit <name> standard"
        person = await finde_nutzer(self._session_fabrik, " ".join(argumente[:-1]))
        name = person.anzeigename or str(person.telegram_id)
        angabe = argumente[-1].lower().replace(",", ".").removesuffix("€")
        if angabe == "standard":
            euro, text = None, f"Tageslimit von {name} auf den Standard zurücksetzen"
        else:
            try:
                euro = Decimal(angabe)
            except InvalidOperation:
                return "Das Limit muss eine Zahl in Euro sein, z. B. /limit Lea 10"
            if not euro.is_finite() or euro < 0 or euro > 1000:
                return "Das Limit muss zwischen 0 und 1000 Euro liegen."
            text = f"Tageslimit von {name} auf {als_euro(euro)} setzen"
        return self._merke_aktion(
            nutzer,
            "limit",
            text,
            {"telegram_id": person.telegram_id, "euro": str(euro) if euro is not None else None},
            lambda: self._kosten.setze_limit(person.id, euro),
        )

    async def _modell(self, nutzer: NutzerKontext, argumente: list[str], *_: object) -> str:
        aktuell = self._vorgaben.hole(nutzer.nutzer_id)
        if not argumente:
            return (
                f"Modellstufe: {aktuell}. Mit auto wählt der Bot selbst: einfach für kurze "
                "Lesefragen, komplex für Fotos, PDFs und Analysen, sonst standard. Ändern: "
                "/modell einfach, /modell standard, /modell komplex oder /modell auto. Die "
                "Wahl gilt bis zum nächsten Neustart des Bots."
            )
        stufe = argumente[0].lower()
        if stufe not in (*STUFEN, AUTO):
            return "Möglich sind: einfach, standard, komplex, auto."
        self._vorgaben.setze(nutzer.nutzer_id, stufe)
        return f"Modellstufe: {stufe}."

    # ---------------------------------------------------------------- Profil und Notizen

    async def _profil(self, nutzer: NutzerKontext, argumente: list[str], *_: object) -> str:
        if not argumente:
            return "\n".join(
                [
                    "Dein Profil:",
                    f"Name: {nutzer.anzeigename or '(nicht gesetzt)'}",
                    f"Anrede: {nutzer.ton}",
                    f"Zeitzone: {nutzer.zeitzone}",
                    f"Rollen: {', '.join(sorted(nutzer.rollen)) or 'keine'}",
                    "Ändern: /profil name <Name>, /profil ton du oder sie, "
                    "/profil zeitzone Europe/Berlin",
                ]
            )
        if len(argumente) < 2:
            return "So geht es: /profil name <Name>, /profil ton du, /profil zeitzone Europe/Berlin"
        feld = argumente[0].lower()
        wert = await aendere_profil(self._session_fabrik, nutzer.id, feld, " ".join(argumente[1:]))
        bezeichnung = {"name": "Name", "ton": "Anrede", "zeitzone": "Zeitzone"}[feld]
        return f"Gespeichert. {bezeichnung}: {wert}"

    async def _merken(self, nutzer: NutzerKontext, argumente: list[str], *_: object) -> str:
        anzahl = await merke(self._session_fabrik, nutzer, " ".join(argumente))
        wort = "Notiz" if anzahl == 1 else "Notizen"
        return f"Gemerkt. Du hast jetzt {anzahl} {wort}. /gemerkt zeigt sie."

    async def _gemerkt(self, nutzer: NutzerKontext, *_: object) -> str:
        notizen = await lade_notizen(self._session_fabrik, nutzer)
        if not notizen:
            return "Du hast noch nichts gespeichert. Mit /merken <text> legst du eine Notiz an."
        return "\n".join(["Deine Notizen:", *(f"{n}. {text}" for n, text in enumerate(notizen, 1))])

    async def _vergessen(self, nutzer: NutzerKontext, *_: object) -> BefehlsAntwort:
        async def ausfuehren() -> None:
            await vergiss_alles(self._session_fabrik, nutzer)

        return self._merke_aktion(
            nutzer,
            "vergessen",
            "deinen gesamten Gesprächsverlauf und alle deine Notizen löschen (nicht umkehrbar)",
            {},
            ausfuehren,
            recht=None,
        )

    # ---------------------------------------------------------------- Hilfe

    async def _start(self, nutzer: NutzerKontext, *_: object) -> str:
        name = f", {nutzer.anzeigename}" if nutzer.anzeigename else ""
        return (
            f"Hallo{name}! Ich bin der Assistent der Grünschwert GmbH. Schreib mir einfach, was "
            "du brauchst. /hilfe zeigt die Befehle."
        )

    async def _hilfe(self, nutzer: NutzerKontext, *_: object) -> str:
        """Nur die Befehle, die diese Person nutzen darf."""
        zeilen = ["Befehle für dich:"]
        zeilen += [f"/{b.name} – {b.beschreibung}" for b in self.erlaubte(nutzer)]
        zeilen += ["", f"Deine Rollen: {', '.join(sorted(nutzer.rollen)) or 'keine'}", GRENZE_TEXT]
        return "\n".join(zeilen)

    # ---------------------------------------------------------------- Nutzerverwaltung

    def _merke_aktion(
        self,
        nutzer: NutzerKontext,
        befehl: str,
        beschreibung: str,
        parameter: dict,
        ausfuehren: Callable[[], Awaitable[None]],
        recht: str | None = ADMIN_NUTZER,
    ) -> BefehlsAntwort:
        kennung = secrets.token_hex(6)
        self._aktionen[kennung] = _Aktion(
            telegram_id=nutzer.telegram_id,
            befehl=befehl,
            beschreibung=beschreibung,
            parameter=parameter,
            ausfuehren=ausfuehren,
            gueltig_bis=time.monotonic() + AKTION_FRIST_SEKUNDEN,
            recht=recht,
        )
        return BefehlsAntwort(f"Bitte bestätigen: {beschreibung}", aktion=kennung)

    async def bestaetige(self, kennung: str, nutzer: NutzerKontext, ja: bool) -> str | None:
        """Klick auf den Bestätigungsbutton. None: Der Klick stammt nicht vom Admin, der die
        Aktion vorbereitet hat, und wird ignoriert."""
        aktion = self._aktionen.get(kennung)
        if aktion is None:
            return "Diese Aktion gibt es nicht mehr."
        if aktion.telegram_id != nutzer.telegram_id:
            return None
        del self._aktionen[kennung]
        if time.monotonic() > aktion.gueltig_bis:
            return "Die Bestätigung ist abgelaufen. Es wurde nichts geändert."
        # Die Rechte werden beim Klick noch einmal geprüft.
        if aktion.recht is not None and not nutzer.darf(aktion.recht):
            return "Dafür fehlt dir das Recht. Es wurde nichts geändert."
        if not ja:
            return f"Abgebrochen: {aktion.beschreibung}"
        try:
            await aktion.ausfuehren()
        except NutzerFehler as exc:
            await protokolliere(
                self._session_fabrik,
                user_id=nutzer.id,
                tool_name=f"/{aktion.befehl}",
                parameter=aktion.parameter,
                fehler=str(exc),
            )
            return f"Das ging nicht: {exc}"
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name=f"/{aktion.befehl}",
            parameter=aktion.parameter,
            ergebnis_kurz="ausgeführt",
        )
        return f"✅ Erledigt: {aktion.beschreibung}"

    async def _nutzer(self, nutzer: NutzerKontext, *_: object) -> str:
        zeilen = ["Personen:"]
        for person in await liste_nutzer(self._session_fabrik):
            zustand = "gesperrt" if person.gesperrt or not person.aktiv else "aktiv"
            zeilen.append(
                f"- {person.anzeigename or '(ohne Namen)'}, Telegram-ID {person.telegram_id}, "
                f"Rollen: {', '.join(person.rollen) or 'keine'}, {zustand}"
            )
        return "\n".join(zeilen)

    async def _nutzer_neu(
        self, nutzer: NutzerKontext, argumente: list[str], *_: object
    ) -> "str | BefehlsAntwort":
        if len(argumente) < 2 or not argumente[0].isdigit():
            return "So geht es: /nutzer_neu <telegram_id> <name>"
        telegram_id, name = int(argumente[0]), " ".join(argumente[1:])
        return self._merke_aktion(
            nutzer,
            "nutzer_neu",
            f"{name} (Telegram-ID {telegram_id}) als Mitarbeiter anlegen",
            {"telegram_id": telegram_id},
            lambda: lege_nutzer_an(self._session_fabrik, telegram_id, name, nutzer.id),
        )

    async def _rolle(
        self, nutzer: NutzerKontext, argumente: list[str], *_: object
    ) -> "str | BefehlsAntwort":
        if len(argumente) < 2 or argumente[-1][:1] not in "+-" or len(argumente[-1]) < 2:
            return (
                "So geht es: /rolle <name> +buchhaltung oder /rolle <name> -buchhaltung. "
                f"Rollen: {', '.join(ROLLEN)}"
            )
        hinzufuegen, rolle = argumente[-1][0] == "+", argumente[-1][1:].lower()
        if rolle not in ROLLEN:
            return f"Die Rolle „{rolle}“ gibt es nicht. Möglich: {', '.join(ROLLEN)}."
        person = await finde_nutzer(self._session_fabrik, " ".join(argumente[:-1]))
        verb = "bekommt" if hinzufuegen else "verliert"
        return self._merke_aktion(
            nutzer,
            "rolle",
            f"{person.anzeigename or person.telegram_id} {verb} die Rolle {rolle}",
            {"telegram_id": person.telegram_id, "rolle": rolle, "hinzufuegen": hinzufuegen},
            lambda: aendere_rolle(self._session_fabrik, person.id, rolle, hinzufuegen, nutzer.id),
        )

    async def _sperre(
        self, nutzer: NutzerKontext, argumente: list[str], gesperrt: bool
    ) -> "str | BefehlsAntwort":
        befehl = "sperren" if gesperrt else "entsperren"
        if not argumente:
            return f"So geht es: /{befehl} <name>"
        person = await finde_nutzer(self._session_fabrik, " ".join(argumente))
        if gesperrt and person.telegram_id == nutzer.telegram_id:
            return "Dich selbst kannst du nicht sperren."
        name = person.anzeigename or str(person.telegram_id)
        return self._merke_aktion(
            nutzer,
            befehl,
            f"{name} sperren (ab sofort keine Antworten mehr)"
            if gesperrt
            else f"{name} entsperren",
            {"telegram_id": person.telegram_id},
            lambda: setze_sperre(self._session_fabrik, person.id, gesperrt),
        )

    async def _sperren(self, nutzer: NutzerKontext, argumente: list[str], *_: object):
        return await self._sperre(nutzer, argumente, True)

    async def _entsperren(self, nutzer: NutzerKontext, argumente: list[str], *_: object):
        return await self._sperre(nutzer, argumente, False)

    # ---------------------------------------------------------------- Zugänge

    def _dienst(self, argumente: list[str]) -> str:
        dienst = argumente[0].lower() if argumente else ""
        if dienst not in DIENSTE:
            raise ToolFehler(f"Bitte den Dienst angeben. Möglich: {', '.join(DIENSTE)}.")
        return dienst

    def _zugang(self) -> Zugaenge:
        if self._zugaenge is None:
            raise ToolFehler("Zugänge sind hier nicht eingerichtet.")
        return self._zugaenge

    def _warte_auf(
        self, chat_id: int, nutzer: NutzerKontext, art: str, geheim: bool = False, **daten: object
    ) -> None:
        self._erwartet[(chat_id, nutzer.telegram_id)] = _Dialog(
            art, time.monotonic() + GEHEIMNIS_FRIST_SEKUNDEN, geheim, daten
        )

    async def _verbinden(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        zugang = self._zugang()
        zugang.pruefe_verfuegbar()
        dienst = self._dienst(argumente)
        if not privat:
            return NUR_PRIVAT_TEXT
        if dienst == MAIL:
            return await self._verbinden_mail(nutzer, argumente[1:], chat_id)
        self._warte_auf(chat_id, nutzer, ART_TOKEN, geheim=True, dienst=dienst)
        return (
            f"Schick mir jetzt als nächste Nachricht {DIENSTE[dienst]}.\n"
            "Ich lösche die Nachricht sofort aus dem Chat, speichere den Token verschlüsselt und "
            "gebe ihn nie an die KI weiter. Mit jedem anderen Befehl brichst du ab."
        )

    async def _verbinden_mail(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int
    ) -> str:
        if not nutzer.darf(MAIL_EIGENE):
            return MAIL_KEIN_RECHT_TEXT
        zugang = self._zugang()
        settings = zugang.settings
        # Optional: eigene Server, falls das Postfach nicht beim Standardanbieter liegt.
        if len(argumente) not in (0, 2):
            return (
                "So geht es: /verbinden mail. Liegt das Postfach bei einem anderen Anbieter: "
                "/verbinden mail <imap-server[:port]> <smtp-server[:port]>"
            )
        server = {}
        if argumente:
            server = {
                "imap": lies_server(argumente[0], settings.mail_imap_port),
                "smtp": lies_server(argumente[1], settings.mail_smtp_port),
            }
        self._warte_auf(chat_id, nutzer, ART_MAIL_ADRESSE, **server)
        zeilen = []
        if not await zugang.postfaecher(nutzer):
            zeilen.append(MAIL_KI_HINWEIS.format(stunden=settings.mail_kontext_ttl_stunden))
        zeilen.append(
            "Schick mir jetzt als nächste Nachricht die E-Mail-Adresse des Postfachs. Danach "
            "frage ich nach dem Passwort. Mit jedem anderen Befehl brichst du ab."
        )
        return "\n".join(zeilen)

    def dialog(self, chat_id: int, telegram_id: int) -> _Dialog | None:
        """Die Eingabe, auf die der Bot von dieser Person in diesem Chat gerade wartet."""
        eintrag = self._erwartet.get((chat_id, telegram_id))
        if eintrag is None:
            return None
        if time.monotonic() > eintrag.gueltig_bis:
            del self._erwartet[(chat_id, telegram_id)]
            return None
        return eintrag

    def erwartet_geheimnis(self, chat_id: int, telegram_id: int) -> str | None:
        """Der Dienst, für den die nächste Nachricht dieser Person ein Geheimnis ist."""
        eintrag = self.dialog(chat_id, telegram_id)
        if eintrag is None or not eintrag.geheim:
            return None
        return MAIL if eintrag.art == ART_MAIL_PASSWORT else eintrag.daten["dienst"]

    def brich_ab(self, chat_id: int, telegram_id: int) -> None:
        self._erwartet.pop((chat_id, telegram_id), None)

    async def nimm_eingabe(self, nutzer: NutzerKontext, chat_id: int, text: str) -> str | None:
        """Verarbeitet eine erwartete Eingabe ohne Geheimnis (Adresse, Name, Signatur). Sie
        geht nicht an das Modell und nicht in den Verlauf. None: Die Nachricht gehört doch
        nicht zum Befehl und wird ganz normal beantwortet."""
        eintrag = self._erwartet.pop((chat_id, nutzer.telegram_id))
        text = text.strip()
        try:
            if eintrag.art == ART_MAIL_ADRESSE:
                return self._nimm_adresse(nutzer, chat_id, text, eintrag)
            if eintrag.art == ART_MAIL_NAME:
                return await self._nimm_namen(nutzer, chat_id, text, eintrag)
            if eintrag.art == ART_SIGNATUR:
                return await self._nimm_signatur(nutzer, text, eintrag)
        except ToolFehler as exc:
            return str(exc)
        return None

    def _nimm_adresse(
        self, nutzer: NutzerKontext, chat_id: int, text: str, eintrag: _Dialog
    ) -> str:
        if not ist_adresse(text):
            return (
                "Das sieht nicht nach einer E-Mail-Adresse aus. Es wurde nichts gespeichert. "
                "Mit /verbinden mail kannst du neu beginnen."
            )
        self._warte_auf(
            chat_id, nutzer, ART_MAIL_PASSWORT, geheim=True, adresse=text, **eintrag.daten
        )
        return (
            "Schick mir jetzt als nächste Nachricht das Passwort dieses Postfachs.\n"
            "Ich lösche die Nachricht sofort aus dem Chat, prüfe die Anmeldung (es wird nichts "
            "gesendet), speichere das Passwort verschlüsselt und gebe es nie an die KI weiter."
        )

    async def _nimm_namen(
        self, nutzer: NutzerKontext, chat_id: int, text: str, eintrag: _Dialog
    ) -> str | None:
        label = eintrag.daten["label"]
        if text.lower().rstrip(".!") in NAME_BEHALTEN:
            return f"Alles klar, das Postfach heißt „{label}“."
        if not text.lower().startswith("name "):
            # Kein Wunsch nach einem anderen Namen: Die Nachricht wird normal beantwortet.
            return None
        try:
            neu = pruefe_label(text[5:])
            postfach = await self._zugang().benenne_mail_um(nutzer, label, neu)
        except ToolFehler as exc:
            self._warte_auf(chat_id, nutzer, ART_MAIL_NAME, label=label)
            return f"{exc} Versuch es noch einmal mit „name <neuer name>“."
        return f"Das Postfach heißt jetzt „{postfach.label}“."

    async def _nimm_signatur(self, nutzer: NutzerKontext, text: str, eintrag: _Dialog) -> str:
        label = eintrag.daten["label"]
        if text.lower() in SIGNATUR_LOESCHEN:
            await self._zugang().setze_signatur(nutzer, label, "")
            return f"Die Signatur von „{label}“ ist gelöscht."
        await self._zugang().setze_signatur(nutzer, label, text)
        return f"Gespeichert. Diese Signatur hänge ich an Entwürfe aus „{label}“ an."

    async def nimm_geheimnis(self, nutzer: NutzerKontext, chat_id: int, geheimnis: str) -> str:
        """Verarbeitet die Nachricht mit Token oder Passwort. Sie wird weder gespeichert noch
        geloggt noch an das Modell gegeben; der Kanal hat sie bereits aus dem Chat gelöscht."""
        eintrag = self._erwartet.pop((chat_id, nutzer.telegram_id))
        if eintrag.art == ART_MAIL_PASSWORT:
            return await self._nimm_passwort(nutzer, chat_id, geheimnis, eintrag)
        dienst = eintrag.daten["dienst"]
        try:
            konto = await self._zugang().verbinde(nutzer, dienst, geheimnis)
        except ToolFehler as exc:
            await protokolliere(
                self._session_fabrik,
                user_id=nutzer.id,
                tool_name="/verbinden",
                parameter={"dienst": dienst},
                fehler="abgelehnt",
            )
            return (
                f"Das hat nicht geklappt: {exc} Es wurde nichts gespeichert. Mit /verbinden "
                f"{dienst} kannst du es noch einmal versuchen."
            )
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name="/verbinden",
            parameter={"dienst": dienst},
            ergebnis_kurz="verbunden",
        )
        return f"✅ {dienst.capitalize()} ist verbunden, Konto: {konto or 'unbekannt'}."

    async def _nimm_passwort(
        self, nutzer: NutzerKontext, chat_id: int, passwort: str, eintrag: _Dialog
    ) -> str:
        daten = eintrag.daten
        try:
            postfach = await self._zugang().verbinde_mail(
                nutzer, daten["adresse"], passwort, daten.get("imap"), daten.get("smtp")
            )
        except ToolFehler as exc:
            await protokolliere(
                self._session_fabrik,
                user_id=nutzer.id,
                tool_name="/verbinden",
                parameter={"dienst": MAIL},
                fehler="abgelehnt",
            )
            return (
                f"Das hat nicht geklappt: {exc} Es wurde nichts gespeichert. Mit /verbinden "
                "mail kannst du es noch einmal versuchen."
            )
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name="/verbinden",
            parameter={"dienst": MAIL, "konto": postfach.label},
            ergebnis_kurz="verbunden",
        )
        self._warte_auf(chat_id, nutzer, ART_MAIL_NAME, label=postfach.label)
        return (
            f"✅ Das Postfach {postfach.adresse} ist verbunden. Bei mir heißt es "
            f"„{postfach.label}“. Möchtest du einen anderen Namen? Dann antworte mit "
            "„name <neuer name>“. Sonst schreib einfach weiter."
        )

    async def _trennen(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        dienst = self._dienst(argumente)
        if dienst == MAIL:
            return await self._trennen_mail(nutzer, argumente[1:])
        entfernt = await self._zugang().trenne(nutzer, dienst)
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name="/trennen",
            parameter={"dienst": dienst},
            ergebnis_kurz="getrennt" if entfernt else "war nicht verbunden",
        )
        if not entfernt:
            return f"{dienst.capitalize()} war nicht verbunden."
        return f"{dienst.capitalize()} ist getrennt. Dein gespeicherter Token ist gelöscht."

    async def _eigenes_label(self, nutzer: NutzerKontext, argumente: list[str], befehl: str) -> str:
        """Das gemeinte eigene Postfach; ohne Angabe das einzige."""
        eigene = [p.label for p in await self._zugang().postfaecher(nutzer)]
        if not eigene:
            raise ToolFehler("Du hast kein Postfach verbunden. Los geht es mit /verbinden mail.")
        if not argumente:
            if len(eigene) == 1:
                return eigene[0]
            raise ToolFehler(f"Welches Postfach? {befehl} <name>. Deine: {', '.join(eigene)}.")
        label = argumente[0].lower()
        if label not in eigene:
            raise ToolFehler(
                f"Ein Postfach „{label}“ hast du nicht verbunden. Deine: {', '.join(eigene)}."
            )
        return label

    async def _trennen_mail(self, nutzer: NutzerKontext, argumente: list[str]) -> str:
        label = await self._eigenes_label(nutzer, argumente, "/trennen mail")
        entfernt = await self._zugang().trenne_mail(nutzer, label)
        await protokolliere(
            self._session_fabrik,
            user_id=nutzer.id,
            tool_name="/trennen",
            parameter={"dienst": MAIL, "konto": label},
            ergebnis_kurz="getrennt" if entfernt else "war nicht verbunden",
        )
        return (
            f"Das Postfach „{label}“ ist getrennt. Das gespeicherte Passwort und die Signatur "
            "sind gelöscht."
        )

    async def _signatur(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        if not nutzer.darf(MAIL_EIGENE):
            return MAIL_KEIN_RECHT_TEXT
        label = await self._eigenes_label(nutzer, argumente, "/signatur")
        postfach = next(p for p in await self._zugang().postfaecher(nutzer) if p.label == label)
        self._warte_auf(chat_id, nutzer, ART_SIGNATUR, label=label)
        aktuell = (
            f"Aktuelle Signatur von „{label}“:\n{postfach.signatur}"
            if postfach.signatur
            else f"Das Postfach „{label}“ hat noch keine Signatur."
        )
        return (
            f"{aktuell}\n\nSchick mir als nächste Nachricht die neue Signatur (mehrere Zeilen "
            "sind möglich) oder „löschen“. Ich hänge sie an jede Mail und jeden Entwurf aus "
            "diesem Postfach an. Mit jedem anderen Befehl brichst du ab."
        )

    async def _verbunden(
        self, nutzer: NutzerKontext, argumente: list[str], chat_id: int, privat: bool
    ) -> str:
        dienste = await self._zugang().liste(nutzer)
        if not dienste:
            return "Du hast noch keinen Dienst verbunden. Los geht es mit /verbinden asana."
        teile = [dienst for dienst in dienste if dienst != MAIL]
        if MAIL in dienste:
            postfaecher = await self._zugang().postfaecher(nutzer)
            teile += [f"Postfach {p.label} ({p.adresse})" for p in postfaecher]
        return "Verbunden: " + ", ".join(teile) + ". Die Zugangsdaten selbst zeige ich nie an."


def rechte_als_text(nutzer: NutzerKontext) -> str:
    """Was die Person darf, in Worten (für Systemprompt und Hilfe)."""
    return "; ".join(RECHTE[recht] for recht in sorted(nutzer.rechte) if recht in RECHTE)
