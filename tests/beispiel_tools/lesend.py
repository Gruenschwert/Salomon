from app.tools.base import BasisTool, ToolFehler, aktueller_nutzer

_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


class BeispielLesen(BasisTool):
    name = "beispiel_lesen"
    beschreibung = "Gibt den Text zurück."
    parameter_schema = _TEXT_SCHEMA

    async def ausfuehren(self, text: str) -> dict:
        return {"echo": text, "nutzer": aktueller_nutzer.get().telegram_id}


class BeispielKaputt(BasisTool):
    name = "beispiel_kaputt"
    beschreibung = "Wirft immer einen unerwarteten Fehler."
    parameter_schema = {"type": "object", "properties": {}}

    async def ausfuehren(self) -> dict:
        raise RuntimeError("geheimes internes Detail")


class BeispielErwartbarerFehler(BasisTool):
    name = "beispiel_fehler"
    beschreibung = "Wirft immer einen erwartbaren Fehler."
    parameter_schema = {"type": "object", "properties": {}}

    async def ausfuehren(self) -> dict:
        raise ToolFehler("Shop nicht konfiguriert")


class BeispielNurAdmin(BasisTool):
    name = "beispiel_admin"
    beschreibung = "Nur für Admins."
    parameter_schema = {"type": "object", "properties": {}}
    erforderliche_rechte = frozenset({"admin.nutzer"})

    async def ausfuehren(self) -> dict:
        return {"ok": True}
