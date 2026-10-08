"""Allgemeiner Asana-API-Aufruf als Operation eines Änderungssatzes, samt Pfadprüfung und
Sperrliste. Das Tool `asana_api_aufruf` (in asana_api.py) baut darauf auf."""

import json
import re

from app.config import Settings
from app.tools.asana_operationen import (
    KATEGORIE_AENDERN,
    LOESCH_MARKE,
    OBJEKT_FELDER,
    WAHL_FELDER,
    ZUSATZ_BESCHREIBUNG,
    ZUSATZ_SCHEMA,
    Lauf,
    OpErgebnis,
    registriere,
)
from app.tools.base import ToolFehler

METHODEN = ("GET", "POST", "PUT", "DELETE")
SCHREIBENDE_METHODEN = ("POST", "PUT", "DELETE")
ROLLEN_EINSTELLUNG = "asana_api_aufruf_roles"
MAX_PFAD_ZEICHEN = 300
MAX_BODY_VORSCHAU = 1500
MAX_ERGEBNIS_ZEICHEN = 300
ALLE = frozenset(METHODEN)
SCHREIBEND = frozenset(SCHREIBENDE_METHODEN)

# Ein Pfad besteht nur aus Segmenten mit Buchstaben, Ziffern, Unterstrich und Bindestrich.
# Damit sind Schema, Host, „..“, Querystring, Prozent-Kodierung und Steuerzeichen ausgeschlossen.
PFAD_MUSTER = re.compile(r"(/[A-Za-z0-9_-]+)+")
SCHLUESSEL_MUSTER = re.compile(r"[A-Za-z0-9_.]+")

# Sperrliste: (Methoden, erstes Pfadsegment, Grund). Sie ist nur durch eine Codeänderung
# erweiterbar oder kürzbar und per Test abgesichert. Geprüft wird in Kleinbuchstaben.
SPERRLISTE: tuple[tuple[frozenset[str], str, str], ...] = (
    (SCHREIBEND, "users", "Änderungen an Nutzern"),
    (SCHREIBEND, "workspaces", "Änderungen an Workspaces"),
    (SCHREIBEND, "workspace_memberships", "Änderungen an Workspace-Mitgliedschaften"),
    (ALLE, "roles", "Rollen"),
    (ALLE, "budgets", "Budgets"),
    (ALLE, "access_requests", "Zugriffsanfragen"),
    (ALLE, "webhooks", "Webhooks"),
    (ALLE, "organization_exports", "Export der ganzen Organisation"),
    (ALLE, "audit_log_events", "Audit-Log der Organisation"),
    (ALLE, "exports", "Massenexport von Daten"),
    # Über /batch ließen sich beliebige andere Aufrufe verpacken, auch gesperrte.
    (ALLE, "batch", "Sammelaufrufe"),
    (frozenset({"POST"}), "attachments", "Datei-Upload (dafür gibt es anhang_hinzufuegen)"),
)
# Token- und Anmelde-Endpunkte sind in jedem Segment und mit jeder Methode gesperrt.
GESPERRTE_WORTTEILE = ("oauth", "token", "auth", "secret", "password", "session")

OBJEKT_FELDER.update({"abfrage", "body"})
WAHL_FELDER["methode"] = METHODEN
ZUSATZ_SCHEMA.update(
    {
        "methode": {"type": "string", "enum": list(SCHREIBENDE_METHODEN)},
        "pfad": {
            "type": "string",
            "description": "Pfad unter https://app.asana.com/api/1.0, z. B. /tasks/123",
        },
        "abfrage": {"type": "object", "description": "Abfrage-Parameter (optional)"},
        "body": {
            "type": "object",
            "description": "Inhalt von „data“ im Request (optional)",
        },
        "begruendung": {"type": "string", "description": "Kurzer Satz, wofür der Aufruf ist"},
    }
)
ZUSATZ_BESCHREIBUNG.append(
    "- api_aufruf: methode (POST, PUT, DELETE), pfad, body, abfrage, begruendung. Auffangnetz "
    "für alles, wofür es keine eigene Operation gibt; nur für Admins. DELETE zählt als Löschung"
)


def pruefe_pfad(pfad: object) -> str:
    """Stellt sicher, dass der Aufruf nur einen Pfad unterhalb der Asana-API treffen kann."""
    if not isinstance(pfad, str) or not pfad or len(pfad) > MAX_PFAD_ZEICHEN:
        raise ToolFehler("„pfad“ fehlt oder ist zu lang.")
    if not PFAD_MUSTER.fullmatch(pfad):
        raise ToolFehler(
            "„pfad“ muss mit / beginnen und darf nur aus Segmenten mit Buchstaben, Ziffern, _ "
            "und - bestehen, z. B. /tasks/123. Kein Host, kein „..“, kein Fragezeichen: "
            "Abfrage-Parameter gehören in „abfrage“."
        )
    return pfad


def pruefe_sperrliste(methode: str, pfad: str) -> None:
    segmente = pfad.lower().strip("/").split("/")
    for segment in segmente:
        if any(teil in segment for teil in GESPERRTE_WORTTEILE):
            raise ToolFehler(
                "Dieser Aufruf ist gesperrt: Token- und Anmelde-Endpunkte sind über den "
                "allgemeinen API-Aufruf nie erreichbar."
            )
    for methoden, anfang, grund in SPERRLISTE:
        if methode in methoden and segmente[0] == anfang:
            raise ToolFehler(
                f"Dieser Aufruf ist gesperrt ({grund}). Das lässt sich nur in Asana selbst "
                "erledigen."
            )


def pruefe_abfrage(abfrage: object) -> dict:
    if abfrage in (None, {}):
        return {}
    if not isinstance(abfrage, dict):
        raise ToolFehler("„abfrage“ muss ein Objekt sein.")
    sauber = {}
    for schluessel, wert in abfrage.items():
        if not isinstance(schluessel, str) or not SCHLUESSEL_MUSTER.fullmatch(schluessel):
            raise ToolFehler(f"Der Abfrage-Parameter „{schluessel}“ ist nicht erlaubt.")
        if isinstance(wert, list):
            wert = ",".join(str(w) for w in wert)
        if isinstance(wert, bool):
            wert = "true" if wert else "false"
        if not isinstance(wert, str | int | float):
            raise ToolFehler(f"Der Wert von „{schluessel}“ muss Text oder Zahl sein.")
        sauber[schluessel] = wert
    return sauber


def pruefe_zugang(settings: Settings) -> None:
    if not settings.asana_api_aufruf_enabled:
        raise ToolFehler(
            "Der allgemeine Asana-API-Aufruf ist abgeschaltet (ASANA_API_AUFRUF_ENABLED=false)."
        )


def pruefe_aufruf(op: dict) -> tuple[str, str, dict]:
    """Prüft Methode, Pfad, Sperrliste und Abfrage. Liefert (Methode, Pfad, Abfrage)."""
    methode = op.get("methode")
    if methode not in METHODEN:
        raise ToolFehler(f"„methode“ muss eine von {', '.join(METHODEN)} sein.")
    pfad = pruefe_pfad(op.get("pfad"))
    pruefe_sperrliste(methode, pfad)
    if methode in ("GET", "DELETE") and op.get("body"):
        raise ToolFehler(f"Ein {methode}-Aufruf hat keinen Body.")
    return methode, pfad, pruefe_abfrage(op.get("abfrage"))


def _lesbar(wert: object, maximum: int) -> str:
    text = json.dumps(wert, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


@registriere(
    "api_aufruf",
    KATEGORIE_AENDERN,
    pflicht={"methode", "pfad", "begruendung"},
    optional={"abfrage", "body"},
    rollen=ROLLEN_EINSTELLUNG,
)
class ApiAufruf:
    @staticmethod
    def _pruefe(op: dict, lauf: Lauf) -> tuple[str, str, dict]:
        pruefe_zugang(lauf.settings)
        methode, pfad, abfrage = pruefe_aufruf(op)
        if methode == "GET":
            raise ToolFehler(
                "Lesende Aufrufe (GET) gehören nicht in einen Änderungssatz. Nutze dafür das "
                "Tool asana_api_aufruf direkt; es braucht keine Freigabe."
            )
        return methode, pfad, abfrage

    @staticmethod
    async def vorschau(op: dict, lauf: Lauf) -> str:
        methode, pfad, abfrage = ApiAufruf._pruefe(op, lauf)
        marke = f"{LOESCH_MARKE} " if methode == "DELETE" else ""
        text = f"{marke}API-Aufruf: {methode} {pfad}, Begründung: {op['begruendung']}"
        if abfrage:
            text += f", Abfrage: {_lesbar(abfrage, MAX_BODY_VORSCHAU)}"
        if "body" in op:
            text += f", Body: {_lesbar(op['body'], MAX_BODY_VORSCHAU)}"
        return text

    @staticmethod
    async def ausfuehren(op: dict, lauf: Lauf) -> OpErgebnis:
        methode, pfad, abfrage = ApiAufruf._pruefe(op, lauf)
        daten = None if methode == "DELETE" else op.get("body", {})
        antwort = (await lauf.asana.roh(methode, pfad, abfrage, daten)).get("data")
        gid = antwort.get("gid", "") if isinstance(antwort, dict) else ""
        text = f"API-Aufruf {methode} {pfad} ausgeführt"
        if antwort:
            text += f", Antwort: {_lesbar(antwort, MAX_ERGEBNIS_ZEICHEN)}"
        return OpErgebnis(
            gid=str(gid),
            text=text,
            felder=tuple(sorted(op.get("body", {}))),
            audit={
                "methode": methode,
                "pfad": pfad,
                **({"abfrage": abfrage} if abfrage else {}),
                **({"body": op["body"]} if "body" in op else {}),
            },
        )
