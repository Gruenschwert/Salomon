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

from app.config import Settings
from app.tools.asana_client import AsanaClient, AsanaFehler, kuerze_text, pruefe_gid
from app.tools.base import ToolFehler

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

BOOL_FELDER = frozenset(
    {"meilenstein", "erledigt", "archiviert", "entfernen", "aus_projekt_entfernen"}
)
LISTEN_FELDER = frozenset({"tags", "follower"})
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
}
# In aufgabe_aendern und projekt_aendern löscht ein leerer Wert das Feld.
LEERBAR = frozenset({"startdatum", "faellig", "faellig_um", "zustaendig_gid"})

_AUFGABE_FELDER = (
    "name",
    "notes",
    "completed",
    "due_on",
    "due_at",
    "start_on",
    "resource_subtype",
    "assignee.name",
    "parent.name",
    "memberships.project.name",
    "memberships.section.name",
    "permalink_url",
)
_PROJEKT_FELDER = ("name", "notes", "due_on", "owner.name", "color", "archived", "permalink_url")


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

    def __init__(self, asana: AsanaClient, settings: Settings) -> None:
        self.asana = asana
        self.settings = settings
        self.zone = ZoneInfo(settings.tz)
        # Platzhalter -> Stellvertreter des noch nicht existierenden Objekts
        self.neu: dict[str, dict] = {}
        self._gelesen: dict[tuple[str, str], dict] = {}

    async def _hole(self, typ: str, ref: str, pfad: str, felder: tuple[str, ...]) -> dict:
        if ref in self.neu:
            return self.neu[ref]
        if (typ, ref) not in self._gelesen:
            self._gelesen[(typ, ref)] = await self.asana.get(f"{pfad}/{ref}", felder=felder)
        return self._gelesen[(typ, ref)]

    def vergiss(self, typ: str, gid: str) -> None:
        """Nach einer Änderung muss der Zustand beim nächsten Mal neu gelesen werden."""
        self._gelesen.pop((typ, gid), None)

    async def projekt(self, ref: str) -> dict:
        return await self._hole("projekt", ref, "/projects", _PROJEKT_FELDER)

    async def abschnitt(self, ref: str) -> dict:
        return await self._hole("abschnitt", ref, "/sections", ("name", "project.name"))

    async def aufgabe(self, ref: str) -> dict:
        return await self._hole("aufgabe", ref, "/tasks", _AUFGABE_FELDER)

    async def tag(self, ref: str) -> dict:
        return await self._hole("tag", ref, "/tags", ("name",))

    async def nutzer(self, gid: str) -> dict:
        return await self._hole("nutzer", gid, "/users", ("name",))

    async def team(self, gid: str) -> dict:
        return await self._hole("team", gid, "/teams", ("name",))


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


OP_TYPEN: dict[str, OpTyp] = {}


def _registriere(
    art: str,
    kategorie: str,
    pflicht: set[str],
    optional: set[str] = frozenset(),
    erzeugt: str | None = None,
    gid_typ: str | None = None,
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
        )
        return klasse

    return dekorator


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
    aenderung = art in ("aufgabe_aendern", "projekt_aendern")
    op: dict = {"operation": art}
    for feld in sorted(typ.pflicht | typ.optional):
        if feld not in roh:
            if feld in typ.pflicht:
                raise ToolFehler(f"Das Feld „{feld}“ fehlt.")
            continue
        wert = _pruefe_wert(feld, roh[feld], leer_erlaubt=aenderung and feld in LEERBAR)
        if wert is None and not (aenderung and feld in LEERBAR):
            if feld in typ.pflicht:
                raise ToolFehler(f"Das Feld „{feld}“ ist leer.")
            continue
        ref_typ = typ.gid_typ if feld == "gid" else REF_FELDER.get(feld)
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


def _pruefe_wert(feld: str, wert: object, leer_erlaubt: bool) -> object:
    if feld in BOOL_FELDER:
        if not isinstance(wert, bool):
            raise ToolFehler(f"„{feld}“ muss true oder false sein.")
        return wert
    if feld in LISTEN_FELDER:
        if not isinstance(wert, list) or not all(isinstance(w, str | int) for w in wert):
            raise ToolFehler(f"„{feld}“ muss eine Liste von GIDs sein.")
        return [str(w).strip() for w in wert] or None
    if wert is None:
        return None
    if isinstance(wert, int) and not isinstance(wert, bool):
        wert = str(wert)
    if not isinstance(wert, str):
        raise ToolFehler(f"„{feld}“ muss ein Text sein.")
    wert = wert.strip()
    if not wert:
        return None
    if feld in ("faellig", "startdatum"):
        _parse_datum(wert, feld)
    elif feld == "faellig_um":
        _parse_zeitpunkt(wert, UTC)
    elif feld == "farbe" and wert not in FARBEN:
        raise ToolFehler(f"Unbekannte Farbe. Erlaubt sind: {', '.join(FARBEN)}.")
    elif feld in ("zustaendig_gid", "besitzer_gid", "team_gid") and wert != ICH:
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
            f"Der Platzhalter {ref} in „{feld}“ steht für {_TYP_NAME[bekannt[ref]]}, "
            f"erwartet wird {_TYP_NAME[ref_typ]}."
        )


_TYP_NAME = {
    "projekt": "ein Projekt",
    "abschnitt": "einen Abschnitt",
    "aufgabe": "eine Aufgabe",
    "tag": "einen Tag",
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

    return {
        feld: ersetze(wert) if feld == "gid" or feld in REF_FELDER else wert
        for feld, wert in op.items()
    }


# --------------------------------------------------------------------------------------
# Hilfen für Texte, Daten und Felder
# --------------------------------------------------------------------------------------


def _parse_datum(wert: str, feld: str) -> date:
    try:
        return date.fromisoformat(wert)
    except ValueError:
        raise ToolFehler(f"„{feld}“ muss ein Datum im Format JJJJ-MM-TT sein.") from None


def _parse_zeitpunkt(wert: str, zone: tzinfo) -> datetime:
    """Zeitpunkte ohne Zeitzone gelten als Ortszeit der konfigurierten Zone."""
    try:
        zeitpunkt = datetime.fromisoformat(wert)
    except ValueError:
        zeitpunkt = None
    # Ein reines Datum hätte genau zehn Zeichen.
    if zeitpunkt is None or len(wert) <= 10:
        raise ToolFehler("„faellig_um“ muss Datum und Uhrzeit enthalten, z. B. 2026-10-15T14:30.")
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=zone)
    return zeitpunkt


def _asana_zeitpunkt(wert: str, zone: ZoneInfo) -> str:
    return _parse_zeitpunkt(wert, zone).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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


def _faellig_text(objekt: dict, zone: ZoneInfo) -> str:
    if objekt.get("due_at"):
        return zeitpunkt_text(objekt["due_at"], zone)
    return datum_text(objekt.get("due_on"))


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


def _aufgaben_daten(op: dict, zone: ZoneInfo) -> dict:
    """Übersetzt die einfachen Aufgabenfelder in den Asana-Body. Leere Werte löschen."""
    daten: dict = {}
    if "name" in op:
        daten["name"] = op["name"]
    if "beschreibung" in op:
        daten["notes"] = op["beschreibung"]
    if "meilenstein" in op:
        daten["resource_subtype"] = "milestone" if op["meilenstein"] else "default_task"
    if "startdatum" in op:
        daten["start_on"] = op["startdatum"]
    if op.get("faellig") and op.get("faellig_um"):
        raise ToolFehler("Bitte nur „faellig“ oder „faellig_um“ angeben, nicht beide.")
    if op.get("faellig_um"):
        daten["due_at"] = _asana_zeitpunkt(op["faellig_um"], zone)
    elif op.get("faellig"):
        daten["due_on"] = op["faellig"]
    elif "faellig" in op or "faellig_um" in op:
        daten["due_on"] = None
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
}


@_registriere(
    "projekt_aendern",
    KATEGORIE_AENDERN,
    pflicht={"gid"},
    optional={"name", "beschreibung", "faellig", "besitzer_gid", "farbe"},
    gid_typ="projekt",
)
class ProjektAendern:
    @staticmethod
    def _daten(op: dict) -> dict:
        daten: dict = {}
        if "name" in op:
            daten["name"] = op["name"]
        if "beschreibung" in op:
            daten["notes"] = op["beschreibung"]
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
    "faellig",
    "faellig_um",
    "zustaendig_gid",
    "tags",
    "follower",
}


def _aufgaben_art(op: dict) -> str:
    if op.get("meilenstein"):
        return "Meilenstein"
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
        if daten.get("start_on") and not (daten.get("due_on") or daten.get("due_at")):
            raise ToolFehler("Ein Startdatum braucht in Asana auch ein Fälligkeitsdatum.")
        teile = []
        ort, _ = await _ort_text(op, lauf)
        if ort:
            teile.append(ort)
        if "uebergeordnet" in op:
            teile.append(f"unter {bez(await lauf.aufgabe(op['uebergeordnet']))}")
        if not ort and "uebergeordnet" not in op:
            teile.append("ohne Projekt")
        if daten.get("start_on"):
            teile.append(f"Start {datum_text(daten['start_on'])}")
        if daten.get("due_at"):
            teile.append(f"fällig {zeitpunkt_text(daten['due_at'], lauf.zone)}")
        elif daten.get("due_on"):
            teile.append(f"fällig {datum_text(daten['due_on'])}")
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
)
class AufgabeAendern:
    @staticmethod
    def _pruefe(op: dict, daten: dict, aufgabe: dict) -> None:
        if len(op) <= 2:
            raise ToolFehler("aufgabe_aendern braucht mindestens ein zu änderndes Feld.")
        if daten.get("start_on"):
            if "due_on" in daten or "due_at" in daten:
                faellig = daten.get("due_on") or daten.get("due_at")
            else:
                faellig = aufgabe.get("due_on") or aufgabe.get("due_at") or aufgabe.get("neu")
            if not faellig:
                raise ToolFehler("Ein Startdatum braucht in Asana auch ein Fälligkeitsdatum.")

    @staticmethod
    def _vorher(aufgabe: dict, daten: dict, op: dict) -> dict:
        vorher = {}
        for feld in daten:
            if feld == "assignee":
                vorher["zustaendig"] = (aufgabe.get("assignee") or {}).get("name", "")
            elif feld == "notes":
                vorher["beschreibung"] = kuerze_text(aufgabe.get("notes"), MAX_VORHER_ZEICHEN)
            elif feld in ("due_on", "due_at"):
                vorher["due_on"] = aufgabe.get("due_on")
                vorher["due_at"] = aufgabe.get("due_at")
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
        daten = _aufgaben_daten(op, lauf.zone)
        AufgabeAendern._pruefe(op, daten, aufgabe)
        teile = []
        for feld, neu in daten.items():
            if feld == "name":
                teile.append(f"Name → {q(neu)}")
            elif feld == "notes":
                teile.append("Beschreibung geändert")
            elif feld == "resource_subtype":
                teile.append("wird Meilenstein" if neu == "milestone" else "wird normale Aufgabe")
            elif feld == "start_on":
                teile.append(f"Start {datum_text(aufgabe.get('start_on'))} → {datum_text(neu)}")
            elif feld == "due_on":
                teile.append(f"Fällig {_faellig_text(aufgabe, lauf.zone)} → {datum_text(neu)}")
            elif feld == "due_at":
                teile.append(
                    f"Fällig {_faellig_text(aufgabe, lauf.zone)} → {zeitpunkt_text(neu, lauf.zone)}"
                )
            elif feld == "assignee":
                alt = (aufgabe.get("assignee") or {}).get("name") or LEER
                teile.append(f"Zuständig {alt} → {await _nutzer_name(lauf, neu) if neu else LEER}")
        if "uebergeordnet" in op:
            teile.append(f"wird Unteraufgabe von {bez(await lauf.aufgabe(op['uebergeordnet']))}")
        if "projekt" in op or "abschnitt" in op:
            teile.append(f"zusätzlich {(await _ort_text(op, lauf))[0]}")
        for tag in op.get("tags", []):
            teile.append(f"+ Tag {bez(await lauf.tag(tag))}")
        for gid in op.get("follower", []):
            teile.append(f"+ Follower {await _nutzer_name(lauf, gid)}")
        return f"Ändern: {bez(aufgabe)}: " + "; ".join(teile)

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        gid = op["gid"]
        aufgabe = await lauf.aufgabe(gid)
        daten = _aufgaben_daten(op, lauf.zone)
        AufgabeAendern._pruefe(op, daten, aufgabe)
        vorher = AufgabeAendern._vorher(aufgabe, daten, op)
        # Eine Operation kann mehrere Aufrufe brauchen. Scheitert ein späterer, wird gemeldet,
        # was davor schon geändert wurde.
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
