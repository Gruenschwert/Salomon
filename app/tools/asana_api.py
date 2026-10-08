"""Allgemeiner Asana-API-Aufruf: Auffangnetz für alles, wofür es keine eigene Funktion gibt."""

from app.auth.rechte import ASANA_API_AUFRUF
from app.tools.asana_client import AsanaClient
from app.tools.asana_ops_api import (
    METHODEN,
    pruefe_aufruf,
    pruefe_zugang,
)
from app.tools.asana_schreiben import AsanaAenderungenAusfuehren
from app.tools.base import BasisTool, ToolFehler, ToolKontext

# Ein Recht, das niemand hat: So verschwindet das abgeschaltete Tool für alle.
ABGESCHALTET = "abgeschaltet"
STANDARD_LIMIT = 50
MAX_LIMIT = 100


class AsanaApiAufruf(BasisTool):
    name = "asana_api_aufruf"
    beschreibung = (
        "Ruft einen beliebigen Endpunkt der Asana-API auf (https://app.asana.com/api/1.0 plus "
        "pfad). Nimm es nur, wenn es für die Aufgabe kein eigenes Asana-Tool und keine eigene "
        "Operation gibt. Prüfe es, bevor du sagst, etwas gehe in Asana nicht. GET läuft sofort "
        "und liefert höchstens eine Seite (limit bis 100; für die nächste Seite den Wert aus "
        "„naechste_seite“ als abfrage.offset angeben). POST und PUT laufen erst nach Freigabe "
        "des Nutzers, DELETE zusätzlich erst nach der zweiten Bestätigung. Gesperrt sind "
        "Änderungen an Nutzern und Workspaces, Rollen, Budgets, Zugriffsanfragen, Webhooks, "
        "Token- und Anmelde-Endpunkte sowie Datei-Uploads. Den Token setzt das System; gib nie "
        "einen an."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "methode": {"type": "string", "enum": list(METHODEN)},
            "pfad": {
                "type": "string",
                "description": "Pfad mit / am Anfang, z. B. /tasks/123/stories. Ohne Host, "
                "ohne Fragezeichen",
            },
            "abfrage": {
                "type": "object",
                "description": 'Abfrage-Parameter, z. B. {"opt_fields": "name,due_on"}',
            },
            "body": {
                "type": "object",
                "description": "Felder des Requests; das System legt sie in „data“",
            },
            "begruendung": {"type": "string", "description": "Kurzer Satz, wofür der Aufruf ist"},
        },
        "required": ["methode", "pfad", "begruendung"],
    }
    # Sicherer Standard: Nur ein geprüfter GET läuft ohne Freigabe.
    schreibend = True
    ergebnis_im_verlauf = True

    def __init__(self, kontext: ToolKontext) -> None:
        super().__init__(kontext)
        # Abgeschaltet taucht das Tool für niemanden in der Tool-Liste auf.
        self.erforderliche_rechte = (
            frozenset({ASANA_API_AUFRUF})
            if kontext.settings.asana_api_aufruf_enabled
            else frozenset({ASANA_API_AUFRUF, ABGESCHALTET})
        )
        self.asana = AsanaClient(kontext)
        # Schreibende Aufrufe laufen als Änderungssatz mit einer Operation und nutzen damit
        # Vorschau, Freigabe, zweite Bestätigung, Limits und Audit-Log des Schreib-Tools.
        self._satz = AsanaAenderungenAusfuehren(kontext)

    @staticmethod
    def _operation(params: dict) -> dict:
        op = {"operation": "api_aufruf", **params}
        if isinstance(op.get("methode"), str):
            op["methode"] = op["methode"].upper()
        return op

    def ist_schreibend(self, params: dict) -> bool:
        methode = params.get("methode")
        return not (isinstance(methode, str) and methode.upper() == "GET")

    async def bereite_vor(self, **params) -> str:
        return await self._satz.bereite_vor(operationen=[self._operation(params)])

    def vorschau(self, **params) -> str:
        raise ToolFehler("Die Vorschau braucht die Prüfung des Aufrufs.")

    def zweite_bestaetigung(self, vorschau_text: str, **params) -> str | None:
        return self._satz.zweite_bestaetigung(vorschau_text, [self._operation(params)])

    def ergebnis_text(self, ergebnis: dict) -> str | None:
        if "operationen" not in ergebnis:
            return None
        eintraege = ergebnis["operationen"]
        if ergebnis.get("status") == "erfolgreich" and len(eintraege) == 1:
            return f"✅ {eintraege[0]['text']}"
        return self._satz.ergebnis_text(ergebnis)

    async def ausfuehren(self, **params) -> dict:
        op = self._operation(params)
        if op.get("methode") != "GET":
            return await self._satz.ausfuehren(operationen=[op])
        pruefe_zugang(self.kontext.settings)
        if not str(op.get("begruendung") or "").strip():
            raise ToolFehler("Bitte in „begruendung“ kurz sagen, wofür der Aufruf ist.")
        _, pfad, abfrage = pruefe_aufruf(op)
        # Höchstens eine Seite. Ein Limit bekommen nur Listen; ein Pfad, der auf eine GID oder
        # „me“ endet, meint ein einzelnes Objekt und kennt den Parameter nicht.
        letztes = pfad.rsplit("/", 1)[1]
        if not (letztes.isdigit() or letztes == "me"):
            try:
                limit = int(abfrage.get("limit", STANDARD_LIMIT))
            except (TypeError, ValueError):
                raise ToolFehler("„limit“ muss eine Zahl sein.") from None
            abfrage["limit"] = max(1, min(limit, MAX_LIMIT))
        antwort = await self.asana.roh("GET", pfad, abfrage)
        ergebnis: dict = {"data": antwort.get("data")}
        if offset := (antwort.get("next_page") or {}).get("offset"):
            ergebnis["naechste_seite"] = offset
        return ergebnis
