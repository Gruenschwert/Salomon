"""Operationen eines Asana-Änderungssatzes: Prüfung, Vorschau und Ausführung.

Enthält selbst kein Tool. Jede Operation hat zwei Funktionen: `vorschau` liest den aktuellen
Zustand und beschreibt, was passieren würde; `ausfuehren` ändert Asana. Schreibzugriffe gibt
es ausschließlich in den `ausfuehren`-Funktionen.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

from app.tools import asana_feldwerte
from app.tools.asana_client import AsanaClient, AsanaFehler, kuerze_text, pruefe_gid
from app.tools.base import ToolFehler, ToolKontext

PLATZHALTER_MUSTER = re.compile(r"^\$[A-Za-z0-9_]{1,30}$")
ICH = "me"
MAX_NAME_ZEICHEN = 80
MAX_KOMMENTAR_VORSCHAU = 200
MAX_VORHER_ZEICHEN = 100
WOCHENTAGE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
LEER = "(leer)"

FARBEN = (
    "dark-pink",
    "dark-green",
    "dark-blue",
    "dark-red",
    "dark-teal",
    "dark-brown",
    "dark-orange",
    "dark-purple",
    "dark-warm-gray",
    "light-pink",
    "light-green",
    "light-blue",
    "light-red",
    "light-teal",
    "light-brown",
    "light-orange",
    "light-purple",
    "light-warm-gray",
    "none",
)

KATEGORIE_ANLEGEN = "anlegen"
KATEGORIE_AENDERN = "ändern"
KATEGORIE_VERSCHIEBEN = "verschieben"
KATEGORIE_KOMMENTIEREN = "kommentieren"
KATEGORIE_LOESCHEN = "löschen"
KATEGORIEN = (
    KATEGORIE_ANLEGEN,
    KATEGORIE_AENDERN,
    KATEGORIE_VERSCHIEBEN,
    KATEGORIE_KOMMENTIEREN,
    KATEGORIE_LOESCHEN,
)

# Typen der Felder. Die Module mit weiteren Operationen tragen ihre Felder hier ein; ein
# Feldname hat in allen Operationen denselben Typ. Alles Übrige ist Text.
BOOL_FELDER = {"meilenstein", "erledigt", "archiviert", "entfernen", "aus_projekt_entfernen"}
# Listen von Texten (GIDs, Namen oder feste Werte)
LISTEN_FELDER = {"tags", "follower"}
ZAHL_FELDER: set[str] = set()
OBJEKT_FELDER: set[str] = {"felder"}
DATUM_FELDER = {"faellig", "startdatum"}
ZEIT_FELDER = {"faellig_um", "startzeit"}
# Felder mit einer GID, die kein Platzhalter sein kann
GID_FELDER: set[str] = set()
# Nutzer-Felder: GID oder „me“
NUTZER_GID_FELDER = {"zustaendig_gid", "besitzer_gid"}
# Felder mit fester Auswahl: Feldname -> erlaubte Werte
WAHL_FELDER: dict[str, tuple[str, ...]] = {}
# Beiträge der Operationen zu Schema und Beschreibung des Schreib-Tools
ZUSATZ_SCHEMA: dict[str, dict] = {}
ZUSATZ_BESCHREIBUNG: list[str] = []
# Felder, die auf ein Asana-Objekt verweisen, mit dem Typ des Objekts. `gid` hängt von der
# Operation ab und steht deshalb im OpTyp.
REF_FELDER = {
    "projekt": "projekt",
    "abschnitt": "abschnitt",
    "vor_abschnitt_gid": "abschnitt",
    "uebergeordnet": "aufgabe",
    "aufgabe_gid": "aufgabe",
    "haengt_ab_von_gid": "aufgabe",
    "tag_gid": "tag",
    "tags": "tag",
    "team_gid": "team",
}
# In aufgabe_aendern und projekt_aendern löscht ein leerer Wert das Feld.
LEERBAR = frozenset({"startdatum", "startzeit", "faellig", "faellig_um", "zustaendig_gid"})

_AUFGABE_FELDER = (
    "name",
    "notes",
    "completed",
    "due_on",
    "due_at",
    "start_on",
    "start_at",
    "resource_subtype",
    "assignee.name",
    "parent.name",
    "memberships.project.name",
    "memberships.section.name",
    "permalink_url",
    "num_subtasks",
    *(f"custom_fields.{feld}" for feld in asana_feldwerte.FELD_FELDER),
)
_PROJEKT_FELDER = (
    "name",
    "notes",
    "due_on",
    "owner.name",
    "color",
    "archived",
    "permalink_url",
    "privacy_setting",
    "default_view",
)
# Sichtbarkeit eines Projekts: unser Wort -> privacy_setting laut Asana-Doku
SICHTBARKEIT = {
    "privat": "private",
    "team": "private_to_team",
    "oeffentlich": "public_to_workspace",
}
ANSICHTEN = ("list", "board", "calendar", "timeline")
WAHL_FELDER["sichtbarkeit"] = tuple(SICHTBARKEIT)
WAHL_FELDER["standardansicht"] = ANSICHTEN
BOOL_FELDER.add("genehmigung")


@dataclass(frozen=True)
class OpErgebnis:
    gid: str
    # Kurzbeschreibung für die Ergebnis-Meldung, z. B. „Aufgabe „X“ angelegt“
    text: str
    link: str = ""
    ist_projekt: bool = False
    felder: tuple[str, ...] = ()
    vorher: dict = field(default_factory=dict)


class Lauf:
    """Zustand während einer Vorschau oder Ausführung: Asana-Zugriff, Zwischenspeicher für
    gelesene Objekte und – nur in der Vorschau – die im Satz neu angelegten Objekte."""

    def __init__(self, asana: AsanaClient, kontext: ToolKontext) -> None:
        self.asana = asana
        self.kontext = kontext
        self.settings = kontext.settings
        self.zone = ZoneInfo(self.settings.tz)
        # Platzhalter -> Stellvertreter des noch nicht existierenden Objekts
        self.neu: dict[str, dict] = {}
        # Für Listen, die je Lauf nur einmal gelesen werden (Nutzer, Felder)
        self.zwischenspeicher: dict = {}
        self._gelesen: dict[tuple[str, str], dict] = {}

    async def hole(self, typ: str, ref: str, pfad: str, felder: tuple[str, ...]) -> dict:
        """Liest ein Objekt einmal je Lauf; `pfad` ist die Sammlung, z. B. „/tasks“."""
        if ref in self.neu:
            return self.neu[ref]
        if (typ, ref) not in self._gelesen:
            self._gelesen[(typ, ref)] = await self.asana.get(f"{pfad}/{ref}", felder=felder)
        return self._gelesen[(typ, ref)]

    def vergiss(self, typ: str, gid: str) -> None:
        """Nach einer Änderung muss der Zustand beim nächsten Mal neu gelesen werden."""
        self._gelesen.pop((typ, gid), None)

    async def projekt(self, ref: str) -> dict:
        return await self.hole("projekt", ref, "/projects", _PROJEKT_FELDER)

    async def abschnitt(self, ref: str) -> dict:
        return await self.hole("abschnitt", ref, "/sections", ("name", "project.name"))

    async def aufgabe(self, ref: str) -> dict:
        return await self.hole("aufgabe", ref, "/tasks", _AUFGABE_FELDER)

    async def tag(self, ref: str) -> dict:
        return await self.hole("tag", ref, "/tags", ("name",))

    async def nutzer(self, gid: str) -> dict:
        return await self.hole("nutzer", gid, "/users", ("name",))

    async def team(self, gid: str) -> dict:
        return await self.hole("team", gid, "/teams", ("name",))

    async def nutzer_aufloesen(self, wert: object) -> dict:
        """Findet einen Nutzer über GID, „me“, Namen oder E-Mail. Rät nicht bei Mehrdeutigkeit."""
        text = str(wert).strip() if isinstance(wert, str | int) else ""
        if not text:
            raise ToolFehler("Ein Nutzer fehlt.")
        if text == ICH or (text.isascii() and text.isdigit()):
            return await self.nutzer(text)
        if "nutzer" not in self.zwischenspeicher:
            alle, _ = await self.asana.liste(
                "/users",
                {"workspace": await self.asana.workspace_gid()},
                felder=("name", "email"),
                max_eintraege=500,
            )
            self.zwischenspeicher["nutzer"] = alle
        alle = self.zwischenspeicher["nutzer"]
        suche = text.casefold()
        treffer = [
            n
            for n in alle
            if suche in ((n.get("name") or "").casefold(), (n.get("email") or "").casefold())
        ] or [
            n
            for n in alle
            if suche in (n.get("name") or "").casefold()
            or suche in (n.get("email") or "").casefold()
        ]
        if len(treffer) == 1:
            return treffer[0]
        if not treffer:
            raise ToolFehler(f"Einen Asana-Nutzer „{text}“ gibt es nicht.")
        auswahl = ", ".join(f"{n.get('name', '')} ({n['gid']})" for n in treffer[:10])
        raise ToolFehler(
            f"Der Nutzer „{text}“ ist nicht eindeutig: {auswahl}. Bitte den Nutzer fragen, wer "
            "gemeint ist, und die GID verwenden."
        )


Vorschau = Callable[[dict, Lauf], Awaitable[str]]
Ausfuehrung = Callable[[dict, Lauf], Awaitable[OpErgebnis]]


@dataclass(frozen=True)
class OpTyp:
    art: str
    kategorie: str
    pflicht: frozenset[str]
    optional: frozenset[str]
    vorschau: Vorschau
    ausfuehren: Ausfuehrung
    # Typ des Objekts, das die Operation anlegt (für Platzhalter)
    erzeugt: str | None = None
    # Typ des Objekts, auf das `gid` verweist
    gid_typ: str | None = None
    # Felder, bei denen ein leerer Wert „löschen“ bedeutet
    leerbar: frozenset[str] = frozenset()
    # Name der Einstellung mit den Rollen, die diese Operation vorschlagen dürfen
    rollen: str | None = None


OP_TYPEN: dict[str, OpTyp] = {}


def registriere(
    art: str,
    kategorie: str,
    pflicht: set[str],
    optional: set[str] = frozenset(),
    erzeugt: str | None = None,
    gid_typ: str | None = None,
    leerbar: set[str] = frozenset(),
    rollen: str | None = None,
) -> Callable[[type], type]:
    def dekorator(klasse: type) -> type:
        OP_TYPEN[art] = OpTyp(
            art=art,
            kategorie=kategorie,
            pflicht=frozenset(pflicht),
            optional=frozenset(optional),
            vorschau=klasse.vorschau,
            ausfuehren=klasse.ausfuehren,
            erzeugt=erzeugt,
            gid_typ=gid_typ,
            leerbar=frozenset(leerbar),
            rollen=rollen,
        )
        return klasse

    return dekorator


_registriere = registriere


def kategorie_von(op: dict) -> str:
    """Kategorie einer Operation; beim allgemeinen API-Aufruf hängt sie von der Methode ab."""
    if op.get("operation") == "api_aufruf":
        return KATEGORIE_LOESCHEN if op.get("methode") == "DELETE" else KATEGORIE_AENDERN
    return OP_TYPEN[op["operation"]].kategorie


# --------------------------------------------------------------------------------------
# Prüfung der Eingaben
# --------------------------------------------------------------------------------------


def pruefe_operationen(operationen: object) -> list[dict]:
    """Prüft Aufbau, Felder und Platzhalter. Liefert bereinigte Operationen."""
    if not isinstance(operationen, list) or not operationen:
        raise ToolFehler("Der Änderungssatz enthält keine Operationen.")
    geprueft: list[dict] = []
    bekannt: dict[str, str] = {}
    for nummer, roh in enumerate(operationen, start=1):
        try:
            op = _pruefe_operation(roh, bekannt)
        except ToolFehler as exc:
            raise ToolFehler(f"Operation {nummer}: {exc}") from None
        geprueft.append(op)
    return geprueft


def _pruefe_operation(roh: object, bekannt: dict[str, str]) -> dict:
    if not isinstance(roh, dict):
        raise ToolFehler("Eine Operation muss ein Objekt sein.")
    art = roh.get("operation")
    typ = OP_TYPEN.get(art) if isinstance(art, str) else None
    if typ is None:
        raise ToolFehler(
            f"Unbekannte Operation „{art}“. Erlaubt sind: {', '.join(sorted(OP_TYPEN))}."
        )
    erlaubt = typ.pflicht | typ.optional | {"operation"}
    if typ.erzeugt:
        erlaubt |= {"platzhalter"}
    if unbekannt := sorted(set(roh) - erlaubt):
        raise ToolFehler(f"{art} kennt die Felder {', '.join(unbekannt)} nicht.")
    op: dict = {"operation": art}
    for feld in sorted(typ.pflicht | typ.optional):
        if feld not in roh:
            if feld in typ.pflicht:
                raise ToolFehler(f"Das Feld „{feld}“ fehlt.")
            continue
        wert = _pruefe_wert(feld, roh[feld])
        if wert is None and feld not in typ.leerbar:
            if feld in typ.pflicht:
                raise ToolFehler(f"Das Feld „{feld}“ ist leer.")
            continue
        ref_typ = typ.gid_typ if feld == "gid" else REF_FELDER.get(feld)
        if typ.kategorie == KATEGORIE_LOESCHEN and ref_typ and str(wert).startswith("$"):
            raise ToolFehler(
                "Gelöscht wird nur mit einer GID aus einem Lese-Tool, nie mit Platzhalter."
            )
        if ref_typ:
            for ref in wert if isinstance(wert, list) else [wert]:
                _pruefe_ref(feld, ref, ref_typ, bekannt)
        op[feld] = wert
    if "platzhalter" in roh and roh["platzhalter"] not in (None, ""):
        platzhalter = roh["platzhalter"]
        if not isinstance(platzhalter, str) or not PLATZHALTER_MUSTER.match(platzhalter):
            raise ToolFehler("Platzhalter müssen wie $p1, $a2 oder $t3 aussehen.")
        if platzhalter in bekannt:
            raise ToolFehler(f"Der Platzhalter {platzhalter} ist doppelt vergeben.")
        bekannt[platzhalter] = typ.erzeugt
        op["platzhalter"] = platzhalter
    return op


def _pruefe_wert(feld: str, wert: object) -> object:
    if feld in BOOL_FELDER:
        if not isinstance(wert, bool):
            raise ToolFehler(f"„{feld}“ muss true oder false sein.")
        return wert
    if feld in LISTEN_FELDER:
        if not isinstance(wert, list) or not all(isinstance(w, str | int) for w in wert):
            raise ToolFehler(f"„{feld}“ muss eine Liste von Texten sein.")
        werte = [str(w).strip() for w in wert if str(w).strip()]
        for eintrag in werte:
            if feld == "follower" and eintrag != ICH:
                pruefe_gid(eintrag, feld)
            if feld in WAHL_FELDER and eintrag not in WAHL_FELDER[feld]:
                raise ToolFehler(
                    f"„{eintrag}“ ist in „{feld}“ nicht erlaubt. Erlaubt sind: "
                    f"{', '.join(WAHL_FELDER[feld])}."
                )
        return werte or None
    if wert is None:
        return None
    if feld in ZAHL_FELDER:
        if isinstance(wert, bool) or not isinstance(wert, int | float):
            raise ToolFehler(f"„{feld}“ muss eine Zahl sein.")
        return wert
    if feld in OBJEKT_FELDER:
        if not isinstance(wert, dict):
            raise ToolFehler(f"„{feld}“ muss ein Objekt sein.")
        return wert or None
    if isinstance(wert, int) and not isinstance(wert, bool):
        wert = str(wert)
    if not isinstance(wert, str):
        raise ToolFehler(f"„{feld}“ muss ein Text sein.")
    wert = wert.strip()
    if not wert:
        return None
    if feld in DATUM_FELDER:
        _parse_datum(wert, feld)
    elif feld in ZEIT_FELDER:
        _parse_zeitpunkt(wert, UTC, feld)
    elif feld == "farbe" and wert not in FARBEN:
        raise ToolFehler(f"Unbekannte Farbe. Erlaubt sind: {', '.join(FARBEN)}.")
    elif feld in WAHL_FELDER and wert not in WAHL_FELDER[feld]:
        raise ToolFehler(
            f"„{wert}“ ist in „{feld}“ nicht erlaubt. Erlaubt sind: {', '.join(WAHL_FELDER[feld])}."
        )
    elif feld in GID_FELDER or (feld in NUTZER_GID_FELDER and wert != ICH):
        pruefe_gid(wert, feld)
    return wert


def _pruefe_ref(feld: str, ref: str, ref_typ: str, bekannt: dict[str, str]) -> None:
    if not ref.startswith("$"):
        pruefe_gid(ref, feld)
        return
    if ref not in bekannt:
        raise ToolFehler(
            f"Der Platzhalter {ref} in „{feld}“ wird von keiner früheren Operation angelegt."
        )
    if bekannt[ref] != ref_typ:
        raise ToolFehler(
            f"Der Platzhalter {ref} in „{feld}“ steht für {TYP_NAME[bekannt[ref]]}, "
            f"erwartet wird {TYP_NAME.get(ref_typ, ref_typ)}."
        )


TYP_NAME = {
    "projekt": "ein Projekt",
    "abschnitt": "einen Abschnitt",
    "aufgabe": "eine Aufgabe",
    "tag": "einen Tag",
    "team": "ein Team",
}


def loese_platzhalter_auf(op: dict, gids: dict[str, str]) -> dict:
    """Ersetzt Platzhalter durch die echten GIDs der inzwischen angelegten Objekte."""

    def ersetze(wert: object) -> object:
        if isinstance(wert, list):
            return [ersetze(w) for w in wert]
        if isinstance(wert, str) and wert.startswith("$"):
            if wert not in gids:
                raise ToolFehler(f"Der Platzhalter {wert} wurde nicht angelegt.")
            return gids[wert]
        return wert

    aufgeloest = {
        feld: ersetze(wert) if feld == "gid" or feld in REF_FELDER else wert
        for feld, wert in op.items()
    }
    # Benutzerdefinierte Felder können über den Platzhalter eines neuen Feldes benannt sein.
    if isinstance(aufgeloest.get("felder"), dict):
        aufgeloest["felder"] = {ersetze(k): v for k, v in aufgeloest["felder"].items()}
    return aufgeloest


# --------------------------------------------------------------------------------------
# Hilfen für Texte, Daten und Felder
# --------------------------------------------------------------------------------------


def _parse_datum(wert: str, feld: str) -> date:
    try:
        return date.fromisoformat(wert)
    except ValueError:
        raise ToolFehler(f"„{feld}“ muss ein Datum im Format JJJJ-MM-TT sein.") from None


def _parse_zeitpunkt(wert: str, zone: tzinfo, feld: str = "faellig_um") -> datetime:
    """Zeitpunkte ohne Zeitzone gelten als Ortszeit der konfigurierten Zone."""
    try:
        zeitpunkt = datetime.fromisoformat(wert)
    except ValueError:
        zeitpunkt = None
    # Ein reines Datum hätte genau zehn Zeichen.
    if zeitpunkt is None or len(wert) <= 10:
        raise ToolFehler(f"„{feld}“ muss Datum und Uhrzeit enthalten, z. B. 2026-10-15T14:30.")
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=zone)
    return zeitpunkt


def _asana_zeitpunkt(wert: str, zone: ZoneInfo, feld: str = "faellig_um") -> str:
    """ISO 8601 in UTC; die Ortszeit wird samt Sommer-/Winterzeit umgerechnet."""
    zeitpunkt = _parse_zeitpunkt(wert, zone, feld)
    return zeitpunkt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def datum_text(wert: str | None) -> str:
    if not wert:
        return LEER
    tag = date.fromisoformat(wert[:10])
    return f"{WOCHENTAGE[tag.weekday()]} {tag:%d.%m.%Y}"


def zeitpunkt_text(wert: str | None, zone: ZoneInfo) -> str:
    if not wert:
        return LEER
    lokal = _parse_zeitpunkt(wert, zone).astimezone(zone)
    return f"{WOCHENTAGE[lokal.weekday()]} {lokal:%d.%m.%Y %H:%M}"


def _termin_text(objekt: dict, zeit_feld: str, datum_feld: str, zone: ZoneInfo) -> str:
    """Start oder Fälligkeit eines Asana-Objekts bzw. -Bodys als lesbarer Text."""
    if objekt.get(zeit_feld):
        return zeitpunkt_text(objekt[zeit_feld], zone)
    return datum_text(objekt.get(datum_feld))


MIT_UHRZEIT = "zeit"
NUR_DATUM = "datum"
# Ein Termin-Wert ist None oder (MIT_UHRZEIT | NUR_DATUM, Wert im Asana-Format).
Termin = tuple[str, str] | None


def _termin_aus_op(
    op: dict, datum_feld: str, zeit_feld: str, zone: ZoneInfo
) -> tuple[bool, Termin]:
    """Liefert, ob die Operation diese Seite (Start oder Ende) anfasst, und den neuen Wert."""
    if op.get(datum_feld) and op.get(zeit_feld):
        raise ToolFehler(f"Bitte nur „{datum_feld}“ oder „{zeit_feld}“ angeben, nicht beide.")
    if op.get(zeit_feld):
        return True, (MIT_UHRZEIT, _asana_zeitpunkt(op[zeit_feld], zone, zeit_feld))
    if op.get(datum_feld):
        return True, (NUR_DATUM, op[datum_feld])
    return datum_feld in op or zeit_feld in op, None


def _termin_aus_bestand(aufgabe: dict, zeit_feld: str, datum_feld: str) -> Termin:
    if aufgabe.get(zeit_feld):
        return MIT_UHRZEIT, aufgabe[zeit_feld]
    if aufgabe.get(datum_feld):
        return NUR_DATUM, aufgabe[datum_feld]
    return None


def _termin_daten(op: dict, zone: ZoneInfo, aufgabe: dict | None = None) -> dict:
    """Start und Fälligkeit für den Asana-Body, vorab geprüft.

    Regeln der Asana-API (developers.asana.com/reference/createtask): start_on und start_at
    nie zusammen, due_on und due_at nie zusammen. Wird ein Start gesetzt oder entfernt, muss
    das Ende im selben Request stehen; start_at verlangt due_at. Gesendet werden deshalb nur
    drei Formen: Zeitfenster (start_at + due_at), Fälligkeit allein (due_at oder due_on) und
    ganze Tage (start_on + due_on). Start ohne Uhrzeit mit Ende mit Uhrzeit lehnt Asana am
    selben Tag ab und wird hier nie gesendet. Ändert eine Operation nur Start oder nur Ende,
    kommt der andere Wert aus dem aktuellen Zustand der Aufgabe.
    """
    start_neu, start = _termin_aus_op(op, "startdatum", "startzeit", zone)
    ende_neu, ende = _termin_aus_op(op, "faellig", "faellig_um", zone)
    if not (start_neu or ende_neu):
        return {}
    # Eine im selben Satz neu angelegte Aufgabe hat noch keinen lesbaren Zustand.
    bestand_bekannt = aufgabe is not None and not aufgabe.get("neu")
    alter_start = altes_ende = None
    if bestand_bekannt:
        alter_start = _termin_aus_bestand(aufgabe, "start_at", "start_on")
        altes_ende = _termin_aus_bestand(aufgabe, "due_at", "due_on")
        if not start_neu:
            start = alter_start
        if not ende_neu:
            ende = altes_ende
    # Bei einer neuen Aufgabe des Satzes ist die nicht angefasste Seite unbekannt.
    ende_bekannt = ende_neu or bestand_bekannt or aufgabe is None

    hinweis = (
        " Der jeweils andere Wert stammt aus dem aktuellen Stand der Aufgabe."
        if bestand_bekannt and not (start_neu and ende_neu)
        else ""
    )
    if start is not None and ende is None and ende_bekannt:
        if not start_neu:
            # Die Fälligkeit wird gelöscht; ohne sie kann Asana keinen Start halten.
            start = None
        else:
            raise ToolFehler(
                "Ein Startdatum oder eine Startzeit braucht in Asana auch eine Fälligkeit."
            )
    if start is not None and ende is not None:
        if start[0] != ende[0]:
            if start[0] == NUR_DATUM:
                grund = (
                    "Der Start hat keine Uhrzeit, das Ende schon. Das lehnt Asana ab. Frage "
                    "den Nutzer nach der Startuhrzeit und sende dann „startzeit“ zusammen mit "
                    "„faellig_um“."
                )
            else:
                grund = (
                    "Der Start hat eine Uhrzeit, das Ende nicht. Asana verlangt dann auch ein "
                    "Ende mit Uhrzeit. Frage den Nutzer nach der Enduhrzeit und sende dann "
                    "„startzeit“ zusammen mit „faellig_um“."
                )
            raise ToolFehler(
                "Start und Fälligkeit müssen beide mit oder beide ohne Uhrzeit angegeben sein. "
                f"{grund} Schreibe die Uhrzeit nicht ersatzweise in die Beschreibung.{hinweis}"
            )
        if start[0] == MIT_UHRZEIT:
            falsch = _parse_zeitpunkt(start[1], zone) >= _parse_zeitpunkt(ende[1], zone)
        else:
            falsch = start[1] > ende[1]
        if falsch:
            raise ToolFehler(f"Der Start muss vor der Fälligkeit liegen.{hinweis}")

    meilenstein = op.get("meilenstein")
    if meilenstein is None and bestand_bekannt:
        meilenstein = aufgabe.get("resource_subtype") == "milestone"
    if start is not None and meilenstein:
        raise ToolFehler("Ein Meilenstein ist ein einzelner Zeitpunkt und kann keinen Start haben.")

    # Das Ende steht immer im Body, sobald ein Start gesetzt oder entfernt wird. Entfernt wird
    # über das Feld, das bisher gesetzt war (start_at mit due_at, sonst start_on).
    daten: dict = {}
    if ende is not None:
        daten["due_at" if ende[0] == MIT_UHRZEIT else "due_on"] = ende[1]
    elif ende_neu:
        daten["due_at" if altes_ende and altes_ende[0] == MIT_UHRZEIT else "due_on"] = None
    if start is not None:
        daten["start_at" if start[0] == MIT_UHRZEIT else "start_on"] = start[1]
    elif start_neu or alter_start is not None:
        mit_uhrzeit = bool(alter_start and alter_start[0] == MIT_UHRZEIT and "due_on" not in daten)
        daten["start_at" if mit_uhrzeit else "start_on"] = None
    return daten


def q(name: str | None) -> str:
    return f"„{kuerze_text(name, MAX_NAME_ZEICHEN)}“"


def bez(objekt: dict) -> str:
    """Name eines Objekts in Anführungszeichen; neue Objekte des Satzes sind markiert."""
    return q(objekt.get("name")) + (" (neu)" if objekt.get("neu") else "")


async def _nutzer_name(lauf: Lauf, gid: str) -> str:
    name = (await lauf.nutzer(gid)).get("name", "")
    return f"{name} (ich)" if gid == ICH else name


def _ort(mitgliedschaften: list[dict]) -> list[dict]:
    return [
        {
            "projekt": (m.get("project") or {}).get("name", ""),
            "abschnitt": (m.get("section") or {}).get("name", ""),
        }
        for m in mitgliedschaften
    ]


def _aufgaben_daten(op: dict, zone: ZoneInfo, aufgabe: dict | None = None) -> dict:
    """Übersetzt die einfachen Aufgabenfelder in den Asana-Body. Leere Werte löschen.

    `aufgabe` ist bei Änderungen der aktuelle Zustand (für Start und Fälligkeit)."""
    daten: dict = {}
    if "name" in op:
        daten["name"] = op["name"]
    if "beschreibung" in op:
        daten["notes"] = op["beschreibung"]
    if op.get("meilenstein") and op.get("genehmigung"):
        raise ToolFehler("Eine Aufgabe ist entweder Meilenstein oder Genehmigung, nicht beides.")
    if op.get("genehmigung"):
        daten["resource_subtype"] = "approval"
    elif "meilenstein" in op or "genehmigung" in op:
        daten["resource_subtype"] = "milestone" if op.get("meilenstein") else "default_task"
    daten.update(_termin_daten(op, zone, aufgabe))
    if "zustaendig_gid" in op:
        daten["assignee"] = op["zustaendig_gid"]
    return daten


async def _ort_text(op: dict, lauf: Lauf) -> tuple[str, dict | None]:
    """Beschreibt Projekt/Abschnitt einer Operation und liefert das Zielprojekt."""
    abschnitt = await lauf.abschnitt(op["abschnitt"]) if "abschnitt" in op else None
    if "projekt" in op:
        projekt = await lauf.projekt(op["projekt"])
        if abschnitt and (abschnitt.get("project") or {}).get("gid") != projekt.get("gid"):
            raise ToolFehler(
                f"Der Abschnitt {bez(abschnitt)} gehört nicht zum Projekt {bez(projekt)}."
            )
    else:
        projekt = abschnitt.get("project") if abschnitt else None
    if projekt is None:
        return "", None
    text = f"in {bez(projekt)}" + (f" / {bez(abschnitt)}" if abschnitt else "")
    return text, projekt


async def _feldwerte(op: dict, lauf: Lauf, aufgabe: dict | None = None) -> list:
    """Benutzerdefinierte Felder einer Aufgaben-Operation als (Feld, API-Wert, Text)."""
    if "felder" not in op:
        return []
    if aufgabe is not None:
        verfuegbar = None if aufgabe.get("neu") else aufgabe.get("custom_fields") or []
        ort = "an dieser Aufgabe"
    else:
        projekt = op.get("projekt")
        if not projekt and "abschnitt" in op:
            projekt = ((await lauf.abschnitt(op["abschnitt"])).get("project") or {}).get("gid")
        ort = "im Projekt der Aufgabe"
        # Bei einem neuen Projekt des Satzes oder ganz ohne Projekt zählt der Workspace.
        if projekt and projekt not in lauf.neu:
            verfuegbar = await asana_feldwerte.projekt_felder(lauf, projekt)
        else:
            verfuegbar = None
    return await asana_feldwerte.loese_felder_auf(lauf, op["felder"], verfuegbar, ort)


def _stellvertreter(op: dict, **extra) -> dict:
    return {"gid": op.get("platzhalter", ""), "name": op.get("name", ""), "neu": True, **extra}


def merke_neu(op: dict, lauf: Lauf, **extra) -> None:
    if "platzhalter" in op:
        lauf.neu[op["platzhalter"]] = _stellvertreter(op, **extra)


# --------------------------------------------------------------------------------------
# Projekte
# --------------------------------------------------------------------------------------


@_registriere(
    "projekt_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"name"},
    optional={"beschreibung", "team_gid", "faellig", "farbe"},
    erzeugt="projekt",
)
class ProjektAnlegen:
    @staticmethod
    def _team(op: dict, lauf: Lauf) -> str:
        return op.get("team_gid") or lauf.settings.asana_default_team_gid.strip()

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        teile = []
        if team := ProjektAnlegen._team(op, lauf):
            teile.append(f"Team {q((await lauf.team(team)).get('name'))}")
        if "faellig" in op:
            teile.append(f"fällig {datum_text(op['faellig'])}")
        if "farbe" in op:
            teile.append(f"Farbe {op['farbe']}")
        if "beschreibung" in op:
            teile.append("mit Beschreibung")
        merke_neu(op, lauf)
        return f"Anlegen: Projekt {q(op['name'])}" + (f" ({', '.join(teile)})" if teile else "")

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = {"workspace": await lauf.asana.workspace_gid(), "name": op["name"]}
        if "beschreibung" in op:
            daten["notes"] = op["beschreibung"]
        if team := ProjektAnlegen._team(op, lauf):
            daten["team"] = team
        if "faellig" in op:
            daten["due_on"] = op["faellig"]
        if "farbe" in op:
            daten["color"] = op["farbe"]
        neu = await lauf.asana.post("/projects", daten, felder=("name", "permalink_url"))
        return OpErgebnis(
            gid=neu["gid"],
            text=f"Projekt {q(op['name'])} angelegt",
            link=neu.get("permalink_url", ""),
            ist_projekt=True,
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


_PROJEKT_LABEL = {
    "name": "Name",
    "notes": "Beschreibung",
    "due_on": "Fällig",
    "owner": "Besitzer",
    "color": "Farbe",
    "privacy_setting": "Sichtbarkeit",
    "default_view": "Standardansicht",
}


@_registriere(
    "projekt_aendern",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={
        "name",
        "beschreibung",
        "notizen",
        "faellig",
        "besitzer_gid",
        "farbe",
        "sichtbarkeit",
        "standardansicht",
    },
    gid_typ="projekt",
    leerbar=LEERBAR,
)
class ProjektAendern:
    @staticmethod
    def _daten(op: dict) -> dict:
        daten: dict = {}
        if "name" in op:
            daten["name"] = op["name"]
        if "beschreibung" in op and "notizen" in op:
            raise ToolFehler("„beschreibung“ und „notizen“ meinen dasselbe; bitte nur eines.")
        if "beschreibung" in op or "notizen" in op:
            daten["notes"] = op.get("beschreibung") or op["notizen"]
        if "sichtbarkeit" in op:
            daten["privacy_setting"] = SICHTBARKEIT[op["sichtbarkeit"]]
        if "standardansicht" in op:
            daten["default_view"] = op["standardansicht"]
        if "faellig" in op:
            daten["due_on"] = op["faellig"]
        if "besitzer_gid" in op:
            daten["owner"] = op["besitzer_gid"]
        if "farbe" in op:
            daten["color"] = op["farbe"]
        if not daten:
            raise ToolFehler("projekt_aendern braucht mindestens ein zu änderndes Feld.")
        return daten

    @staticmethod
    def _vorher(projekt: dict, daten: dict) -> dict:
        vorher = {}
        for feld in daten:
            if feld == "owner":
                vorher["besitzer"] = (projekt.get("owner") or {}).get("name", "")
            elif feld == "notes":
                vorher["beschreibung"] = kuerze_text(projekt.get("notes"), MAX_VORHER_ZEICHEN)
            else:
                vorher[feld] = projekt.get(feld)
        return vorher

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt = await lauf.projekt(op["gid"])
        teile = []
        for feld, neu in ProjektAendern._daten(op).items():
            label = _PROJEKT_LABEL[feld]
            if feld == "notes":
                teile.append("Beschreibung geändert")
            elif feld == "due_on":
                teile.append(f"{label} {datum_text(projekt.get('due_on'))} → {datum_text(neu)}")
            elif feld == "owner":
                alt = (projekt.get("owner") or {}).get("name") or LEER
                teile.append(f"{label} {alt} → {await _nutzer_name(lauf, neu)}")
            else:
                teile.append(f"{label} {projekt.get(feld) or LEER} → {neu}")
        return f"Ändern: Projekt {bez(projekt)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = ProjektAendern._daten(op)
        projekt = await lauf.projekt(op["gid"])
        vorher = ProjektAendern._vorher(projekt, daten)
        await lauf.asana.put(f"/projects/{op['gid']}", daten)
        lauf.vergiss("projekt", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Projekt {q(projekt.get('name'))} geändert",
            link=projekt.get("permalink_url", ""),
            ist_projekt=True,
            felder=tuple(sorted(set(op) - {"operation", "gid"})),
            vorher=vorher,
        )


@_registriere(
    "projekt_archivieren",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={"archiviert"},
    gid_typ="projekt",
)
class ProjektArchivieren:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt = await lauf.projekt(op["gid"])
        archivieren = op.get("archiviert", True)
        text = "Archivieren" if archivieren else "Wiederherstellen"
        hinweis = ""
        if bool(projekt.get("archived")) == archivieren and not projekt.get("neu"):
            hinweis = " (ist bereits archiviert)" if archivieren else " (ist nicht archiviert)"
        return f"{text}: Projekt {bez(projekt)}{hinweis}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        projekt = await lauf.projekt(op["gid"])
        archivieren = op.get("archiviert", True)
        await lauf.asana.put(f"/projects/{op['gid']}", {"archived": archivieren})
        lauf.vergiss("projekt", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Projekt {q(projekt.get('name'))} "
            + ("archiviert" if archivieren else "wiederhergestellt"),
            link=projekt.get("permalink_url", ""),
            ist_projekt=True,
            felder=("archiviert",),
            vorher={"archiviert": bool(projekt.get("archived"))},
        )


# --------------------------------------------------------------------------------------
# Abschnitte
# --------------------------------------------------------------------------------------


@_registriere(
    "abschnitt_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"projekt", "name"},
    optional={"vor_abschnitt_gid"},
    erzeugt="abschnitt",
)
class AbschnittAnlegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt = await lauf.projekt(op["projekt"])
        text = f"Anlegen: Abschnitt {q(op['name'])} in Projekt {bez(projekt)}"
        if "vor_abschnitt_gid" in op:
            text += f", vor {bez(await lauf.abschnitt(op['vor_abschnitt_gid']))}"
        merke_neu(
            op,
            lauf,
            project={
                "gid": op["projekt"],
                "name": projekt.get("name", ""),
                "neu": bool(projekt.get("neu")),
            },
        )
        return text

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = {"name": op["name"]}
        if "vor_abschnitt_gid" in op:
            daten["insert_before"] = op["vor_abschnitt_gid"]
        neu = await lauf.asana.post(f"/projects/{op['projekt']}/sections", daten)
        return OpErgebnis(
            gid=neu["gid"], text=f"Abschnitt {q(op['name'])} angelegt", felder=("name",)
        )


@_registriere(
    "abschnitt_umbenennen", KATEGORIE_AENDERN, pflicht={"gid", "name"}, gid_typ="abschnitt"
)
class AbschnittUmbenennen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        abschnitt = await lauf.abschnitt(op["gid"])
        projekt = abschnitt.get("project") or {}
        return (
            f"Umbenennen: Abschnitt {bez(abschnitt)} → {q(op['name'])} "
            f"(Projekt {q(projekt.get('name'))})"
        )

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        abschnitt = await lauf.abschnitt(op["gid"])
        await lauf.asana.put(f"/sections/{op['gid']}", {"name": op["name"]})
        lauf.vergiss("abschnitt", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Abschnitt {q(abschnitt.get('name'))} in {q(op['name'])} umbenannt",
            felder=("name",),
            vorher={"name": abschnitt.get("name", "")},
        )


# --------------------------------------------------------------------------------------
# Aufgaben
# --------------------------------------------------------------------------------------

_AUFGABEN_FELDER_OP = {
    "name",
    "beschreibung",
    "projekt",
    "abschnitt",
    "uebergeordnet",
    "meilenstein",
    "startdatum",
    "startzeit",
    "faellig",
    "faellig_um",
    "zustaendig_gid",
    "tags",
    "follower",
    "felder",
    "genehmigung",
}


def _aufgaben_art(op: dict) -> str:
    if op.get("meilenstein"):
        return "Meilenstein"
    if op.get("genehmigung"):
        return "Genehmigung"
    return "Unteraufgabe" if "uebergeordnet" in op else "Aufgabe"


@_registriere(
    "aufgabe_anlegen",
    KATEGORIE_ANLEGEN,
    pflicht={"name"},
    optional=_AUFGABEN_FELDER_OP - {"name"},
    erzeugt="aufgabe",
)
class AufgabeAnlegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        daten = _aufgaben_daten(op, lauf.zone)
        teile = []
        ort, _ = await _ort_text(op, lauf)
        if ort:
            teile.append(ort)
        if "uebergeordnet" in op:
            teile.append(f"unter {bez(await lauf.aufgabe(op['uebergeordnet']))}")
        if not ort and "uebergeordnet" not in op:
            teile.append("ohne Projekt")
        if daten.get("start_at") or daten.get("start_on"):
            teile.append(f"Start {_termin_text(daten, 'start_at', 'start_on', lauf.zone)}")
        if daten.get("due_at") or daten.get("due_on"):
            teile.append(f"fällig {_termin_text(daten, 'due_at', 'due_on', lauf.zone)}")
        if daten.get("assignee"):
            teile.append(f"zuständig {await _nutzer_name(lauf, daten['assignee'])}")
        else:
            teile.append("ohne Zuständigen")
        if "tags" in op:
            namen = [bez(await lauf.tag(tag)) for tag in op["tags"]]
            teile.append("Tags " + ", ".join(namen))
        if "follower" in op:
            namen = [await _nutzer_name(lauf, gid) for gid in op["follower"]]
            teile.append("Follower " + ", ".join(namen))
        for feld, _, text in await _feldwerte(op, lauf):
            teile.append(f"Feld {q(feld.get('name'))}: {text}")
        if "beschreibung" in op:
            teile.append("mit Beschreibung")
        merke_neu(op, lauf, memberships=[])
        return f"Anlegen: {_aufgaben_art(op)} {q(op['name'])} " + ", ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = _aufgaben_daten(op, lauf.zone)
        projekt = op.get("projekt")
        if "abschnitt" in op:
            if not projekt:
                projekt = (await lauf.abschnitt(op["abschnitt"]))["project"]["gid"]
            daten["memberships"] = [{"project": projekt, "section": op["abschnitt"]}]
        elif projekt:
            daten["projects"] = [projekt]
        if "uebergeordnet" in op:
            daten["parent"] = op["uebergeordnet"]
        if not projekt and "uebergeordnet" not in op:
            daten["workspace"] = await lauf.asana.workspace_gid()
        if "tags" in op:
            daten["tags"] = op["tags"]
        if "follower" in op:
            daten["followers"] = op["follower"]
        if werte := await _feldwerte(op, lauf):
            daten["custom_fields"] = {feld["gid"]: wert for feld, wert, _ in werte}
        neu = await lauf.asana.post("/tasks", daten, felder=("name", "permalink_url"))
        return OpErgebnis(
            gid=neu["gid"],
            text=f"{_aufgaben_art(op)} {q(op['name'])} angelegt",
            link=neu.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "platzhalter"})),
        )


@_registriere(
    "aufgabe_aendern",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional=_AUFGABEN_FELDER_OP,
    gid_typ="aufgabe",
    leerbar=LEERBAR,
)
class AufgabeAendern:
    @staticmethod
    def _pruefe(op: dict) -> None:
        if len(op) <= 2:
            raise ToolFehler("aufgabe_aendern braucht mindestens ein zu änderndes Feld.")

    @staticmethod
    def _vorher(aufgabe: dict, daten: dict, op: dict) -> dict:
        vorher = {}
        for feld in daten:
            if feld == "custom_fields":
                continue
            if feld == "assignee":
                vorher["zustaendig"] = (aufgabe.get("assignee") or {}).get("name", "")
            elif feld == "notes":
                vorher["beschreibung"] = kuerze_text(aufgabe.get("notes"), MAX_VORHER_ZEICHEN)
            elif feld in ("due_on", "due_at"):
                vorher["due_on"] = aufgabe.get("due_on")
                vorher["due_at"] = aufgabe.get("due_at")
            elif feld in ("start_on", "start_at"):
                vorher["start_on"] = aufgabe.get("start_on")
                vorher["start_at"] = aufgabe.get("start_at")
            else:
                vorher[feld] = aufgabe.get(feld)
        if "uebergeordnet" in op:
            vorher["uebergeordnet"] = (aufgabe.get("parent") or {}).get("name", "")
        if "projekt" in op or "abschnitt" in op:
            vorher["projekte"] = _ort(aufgabe.get("memberships") or [])
        return vorher

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        AufgabeAendern._pruefe(op)
        daten = _aufgaben_daten(op, lauf.zone, aufgabe)
        teile = []
        for feld, neu in daten.items():
            if feld == "name":
                teile.append(f"Name → {q(neu)}")
            elif feld == "notes":
                teile.append("Beschreibung geändert")
            elif feld == "resource_subtype":
                teile.append(
                    {"milestone": "wird Meilenstein", "approval": "wird Genehmigung"}.get(
                        neu, "wird normale Aufgabe"
                    )
                )
            elif feld == "assignee":
                alt = (aufgabe.get("assignee") or {}).get("name") or LEER
                teile.append(f"Zuständig {alt} → {await _nutzer_name(lauf, neu) if neu else LEER}")
        # Start und Fälligkeit: Ein nur übernommener Wert ist keine Änderung und wird nicht gezeigt.
        for label, zeit_feld, datum_feld in (
            ("Start", "start_at", "start_on"),
            ("Fällig", "due_at", "due_on"),
        ):
            if zeit_feld in daten or datum_feld in daten:
                alt = _termin_text(aufgabe, zeit_feld, datum_feld, lauf.zone)
                neu = _termin_text(daten, zeit_feld, datum_feld, lauf.zone)
                if alt != neu:
                    teile.append(f"{label} {alt} → {neu}")
        for feld, _, text in await _feldwerte(op, lauf, aufgabe):
            alt = feld.get("display_value") or LEER
            teile.append(f"Feld {q(feld.get('name'))}: {alt} → {text}")
        if "uebergeordnet" in op:
            teile.append(f"wird Unteraufgabe von {bez(await lauf.aufgabe(op['uebergeordnet']))}")
        if "projekt" in op or "abschnitt" in op:
            teile.append(f"zusätzlich {(await _ort_text(op, lauf))[0]}")
        for tag in op.get("tags", []):
            teile.append(f"+ Tag {bez(await lauf.tag(tag))}")
        for gid in op.get("follower", []):
            teile.append(f"+ Follower {await _nutzer_name(lauf, gid)}")
        return f"Ändern: {bez(aufgabe)}: " + ("; ".join(teile) or "keine Änderung nötig")

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        gid = op["gid"]
        aufgabe = await lauf.aufgabe(gid)
        AufgabeAendern._pruefe(op)
        daten = _aufgaben_daten(op, lauf.zone, aufgabe)
        vorher = AufgabeAendern._vorher(aufgabe, daten, op)
        # Eine Operation kann mehrere Aufrufe brauchen. Scheitert ein späterer, wird gemeldet,
        # was davor schon geändert wurde.
        if werte := await _feldwerte(op, lauf, aufgabe):
            daten["custom_fields"] = {feld["gid"]: wert for feld, wert, _ in werte}
            vorher["felder"] = {
                feld.get("name", ""): feld.get("display_value") for feld, *_ in werte
            }
        erledigt: list[str] = []
        try:
            if daten:
                await lauf.asana.put(f"/tasks/{gid}", daten)
                erledigt.append("Felder")
            if "uebergeordnet" in op:
                await lauf.asana.post(f"/tasks/{gid}/setParent", {"parent": op["uebergeordnet"]})
                erledigt.append("übergeordnete Aufgabe")
            if "projekt" in op or "abschnitt" in op:
                projekt = op.get("projekt")
                if not projekt:
                    projekt = (await lauf.abschnitt(op["abschnitt"]))["project"]["gid"]
                ziel = {"project": projekt}
                if "abschnitt" in op:
                    ziel["section"] = op["abschnitt"]
                await lauf.asana.post(f"/tasks/{gid}/addProject", ziel)
                erledigt.append("Projekt")
            for tag in op.get("tags", []):
                await lauf.asana.post(f"/tasks/{gid}/addTag", {"tag": tag})
                erledigt.append("Tag")
            if "follower" in op:
                await lauf.asana.post(f"/tasks/{gid}/addFollowers", {"followers": op["follower"]})
        except AsanaFehler as exc:
            lauf.vergiss("aufgabe", gid)
            if erledigt:
                raise AsanaFehler(
                    f"{exc} (bereits geändert: {', '.join(erledigt)})", status=exc.status
                ) from None
            raise
        lauf.vergiss("aufgabe", gid)
        return OpErgebnis(
            gid=gid,
            text=f"{q(aufgabe.get('name'))} geändert",
            link=aufgabe.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "gid"})),
            vorher=vorher,
        )


@_registriere(
    "aufgabe_erledigen",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={"erledigt"},
    gid_typ="aufgabe",
)
class AufgabeErledigen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        erledigt = op.get("erledigt", True)
        hinweis = ""
        if bool(aufgabe.get("completed")) == erledigt and not aufgabe.get("neu"):
            hinweis = " (ist bereits erledigt)" if erledigt else " (ist bereits offen)"
        return f"{'Erledigen' if erledigt else 'Wieder öffnen'}: {bez(aufgabe)}{hinweis}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        erledigt = op.get("erledigt", True)
        await lauf.asana.put(f"/tasks/{op['gid']}", {"completed": erledigt})
        lauf.vergiss("aufgabe", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"{q(aufgabe.get('name'))} " + ("erledigt" if erledigt else "wieder geöffnet"),
            link=aufgabe.get("permalink_url", ""),
            felder=("erledigt",),
            vorher={"erledigt": bool(aufgabe.get("completed"))},
        )


@_registriere(
    "aufgabe_verschieben",
    KATEGORIE_VERSCHIEBEN,
    pflicht={"gid"},
    optional={"projekt", "abschnitt", "aus_projekt_entfernen"},
    gid_typ="aufgabe",
)
class AufgabeVerschieben:
    @staticmethod
    async def _plan(op: dict, lauf: Lauf) -> tuple[dict, dict, dict | None, dict | None]:
        """Liefert Aufgabe, Zielprojekt, Zielabschnitt und das zu verlassende Projekt."""
        if "projekt" not in op and "abschnitt" not in op:
            raise ToolFehler("aufgabe_verschieben braucht „projekt“ oder „abschnitt“.")
        aufgabe = await lauf.aufgabe(op["gid"])
        _, projekt = await _ort_text(op, lauf)
        abschnitt = await lauf.abschnitt(op["abschnitt"]) if "abschnitt" in op else None
        verlassen = None
        if op.get("aus_projekt_entfernen"):
            andere = [
                m["project"]
                for m in aufgabe.get("memberships") or []
                if m.get("project") and m["project"].get("gid") != projekt.get("gid")
            ]
            if len(andere) > 1:
                namen = ", ".join(q(p.get("name")) for p in andere)
                raise ToolFehler(
                    f"Die Aufgabe {bez(aufgabe)} liegt in mehreren Projekten ({namen}). "
                    "Es ist nicht eindeutig, aus welchem sie entfernt werden soll. Bitte den "
                    "Nutzer fragen und ohne aus_projekt_entfernen verschieben."
                )
            verlassen = andere[0] if andere else None
        return aufgabe, projekt, abschnitt, verlassen

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe, projekt, abschnitt, verlassen = await AufgabeVerschieben._plan(op, lauf)
        bisher = next(
            (
                m
                for m in aufgabe.get("memberships") or []
                if (m.get("project") or {}).get("gid") == projekt.get("gid")
            ),
            None,
        )
        if bisher and abschnitt:
            alt = (bisher.get("section") or {}).get("name")
            text = f"Abschnitt {q(alt) if alt else LEER} → {bez(abschnitt)}"
        elif bisher:
            text = f"ist bereits in Projekt {bez(projekt)}"
        elif verlassen:
            text = f"Projekt {q(verlassen.get('name'))} → {bez(projekt)}"
            text += f" / {bez(abschnitt)}" if abschnitt else ""
        else:
            text = f"zusätzlich in Projekt {bez(projekt)}"
            text += f" / {bez(abschnitt)}" if abschnitt else ""
        if verlassen and bisher:
            text += f"; wird aus Projekt {q(verlassen.get('name'))} entfernt"
        return f"Verschieben: {bez(aufgabe)}: {text}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe, projekt, abschnitt, verlassen = await AufgabeVerschieben._plan(op, lauf)
        gid = op["gid"]
        ziel = {"project": projekt["gid"]}
        if abschnitt:
            ziel["section"] = abschnitt["gid"]
        await lauf.asana.post(f"/tasks/{gid}/addProject", ziel)
        lauf.vergiss("aufgabe", gid)
        if verlassen:
            try:
                await lauf.asana.post(f"/tasks/{gid}/removeProject", {"project": verlassen["gid"]})
            except AsanaFehler as exc:
                raise AsanaFehler(
                    f"{exc} (bereits verschoben, aber nicht aus {q(verlassen.get('name'))} "
                    "entfernt)",
                    status=exc.status,
                ) from None
        return OpErgebnis(
            gid=gid,
            text=f"{q(aufgabe.get('name'))} verschoben",
            link=aufgabe.get("permalink_url", ""),
            felder=tuple(sorted(set(op) - {"operation", "gid"})),
            vorher={"projekte": _ort(aufgabe.get("memberships") or [])},
        )


@_registriere("kommentar_hinzufuegen", KATEGORIE_KOMMENTIEREN, pflicht={"aufgabe_gid", "text"})
class KommentarHinzufuegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        return f"Kommentar zu {bez(aufgabe)}: „{kuerze_text(op['text'], MAX_KOMMENTAR_VORSCHAU)}“"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        await lauf.asana.post(f"/tasks/{op['aufgabe_gid']}/stories", {"text": op["text"]})
        return OpErgebnis(
            gid=op["aufgabe_gid"],
            text=f"Kommentar zu {q(aufgabe.get('name'))} hinzugefügt",
            link=aufgabe.get("permalink_url", ""),
            felder=("text",),
        )


# --------------------------------------------------------------------------------------
# Tags und Abhängigkeiten
# --------------------------------------------------------------------------------------


@_registriere("tag_anlegen", KATEGORIE_ANLEGEN, pflicht={"name"}, optional={"farbe"}, erzeugt="tag")
class TagAnlegen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        merke_neu(op, lauf)
        farbe = f" (Farbe {op['farbe']})" if "farbe" in op else ""
        return f"Anlegen: Tag {q(op['name'])}{farbe}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        daten = {"workspace": await lauf.asana.workspace_gid(), "name": op["name"]}
        if "farbe" in op:
            daten["color"] = op["farbe"]
        neu = await lauf.asana.post("/tags", daten)
        return OpErgebnis(gid=neu["gid"], text=f"Tag {q(op['name'])} angelegt", felder=("name",))


@_registriere(
    "tag_zuweisen",
    KATEGORIE_AENDERN,
    pflicht={"aufgabe_gid", "tag_gid"},
    optional={"entfernen"},
)
class TagZuweisen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        tag = await lauf.tag(op["tag_gid"])
        verb = "entfernen" if op.get("entfernen") else "zuweisen"
        return f"Tag {bez(tag)} {verb}: {bez(aufgabe)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        entfernen = op.get("entfernen", False)
        pfad = "removeTag" if entfernen else "addTag"
        await lauf.asana.post(f"/tasks/{op['aufgabe_gid']}/{pfad}", {"tag": op["tag_gid"]})
        return OpErgebnis(
            gid=op["aufgabe_gid"],
            text=f"Tag bei {q(aufgabe.get('name'))} " + ("entfernt" if entfernen else "zugewiesen"),
            link=aufgabe.get("permalink_url", ""),
            felder=("tags",),
        )


@_registriere(
    "abhaengigkeit_setzen",
    KATEGORIE_AENDERN,
    pflicht={"aufgabe_gid", "haengt_ab_von_gid"},
    optional={"entfernen"},
)
class AbhaengigkeitSetzen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        if op["aufgabe_gid"] == op["haengt_ab_von_gid"]:
            raise ToolFehler("Eine Aufgabe kann nicht von sich selbst abhängen.")
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        andere = await lauf.aufgabe(op["haengt_ab_von_gid"])
        if op.get("entfernen"):
            return f"Abhängigkeit entfernen: {bez(aufgabe)} wartet nicht mehr auf {bez(andere)}"
        return f"Abhängigkeit: {bez(aufgabe)} wartet auf {bez(andere)}"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["aufgabe_gid"])
        entfernen = op.get("entfernen", False)
        pfad = "removeDependencies" if entfernen else "addDependencies"
        await lauf.asana.post(
            f"/tasks/{op['aufgabe_gid']}/{pfad}", {"dependencies": [op["haengt_ab_von_gid"]]}
        )
        return OpErgebnis(
            gid=op["aufgabe_gid"],
            text=f"Abhängigkeit bei {q(aufgabe.get('name'))} "
            + ("entfernt" if entfernen else "gesetzt"),
            link=aufgabe.get("permalink_url", ""),
            felder=("abhaengigkeit",),
        )


# --------------------------------------------------------------------------------------
# Löschen (läuft nur nach der zweiten Bestätigung)
# --------------------------------------------------------------------------------------

LOESCH_MARKE = "🗑 Löschen:"


def ist_loeschung(op: dict) -> bool:
    return kategorie_von(op) == KATEGORIE_LOESCHEN


@_registriere("projekt_loeschen", KATEGORIE_LOESCHEN, pflicht={"gid"}, gid_typ="projekt")
class ProjektLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        projekt = await lauf.projekt(op["gid"])
        return f"{LOESCH_MARKE} Projekt {bez(projekt)} samt allen Aufgaben darin"

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        projekt = await lauf.projekt(op["gid"])
        await lauf.asana.delete(f"/projects/{op['gid']}")
        lauf.vergiss("projekt", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Projekt {q(projekt.get('name'))} gelöscht",
            vorher={"name": projekt.get("name", "")},
        )


@_registriere("abschnitt_loeschen", KATEGORIE_LOESCHEN, pflicht={"gid"}, gid_typ="abschnitt")
class AbschnittLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        abschnitt = await lauf.abschnitt(op["gid"])
        projekt = abschnitt.get("project") or {}
        text = f"{LOESCH_MARKE} Abschnitt {bez(abschnitt)} (Projekt {q(projekt.get('name'))})"
        # Asana löscht nur leere Abschnitte.
        aufgaben, weitere = await lauf.asana.liste(f"/sections/{op['gid']}/tasks", max_eintraege=50)
        if aufgaben:
            anzahl = "mehr als 50" if weitere else str(len(aufgaben))
            text += (
                f" – ⚠️ der Abschnitt ist nicht leer ({anzahl} Aufgaben); Asana wird das "
                "Löschen ablehnen, solange die Aufgaben nicht verschoben oder gelöscht sind"
            )
        return text

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        abschnitt = await lauf.abschnitt(op["gid"])
        await lauf.asana.delete(f"/sections/{op['gid']}")
        lauf.vergiss("abschnitt", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Abschnitt {q(abschnitt.get('name'))} gelöscht",
            vorher={"name": abschnitt.get("name", "")},
        )


@_registriere("aufgabe_loeschen", KATEGORIE_LOESCHEN, pflicht={"gid"}, gid_typ="aufgabe")
class AufgabeLoeschen:
    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        aufgabe = await lauf.aufgabe(op["gid"])
        text = f"{LOESCH_MARKE} Aufgabe {bez(aufgabe)}"
        if unteraufgaben := aufgabe.get("num_subtasks"):
            text += f" samt {unteraufgaben} Unteraufgaben"
        return text

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        aufgabe = await lauf.aufgabe(op["gid"])
        await lauf.asana.delete(f"/tasks/{op['gid']}")
        lauf.vergiss("aufgabe", op["gid"])
        return OpErgebnis(
            gid=op["gid"],
            text=f"Aufgabe {q(aufgabe.get('name'))} gelöscht",
            vorher={
                "name": aufgabe.get("name", ""),
                "projekte": _ort(aufgabe.get("memberships") or []),
            },
        )
