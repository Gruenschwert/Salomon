"""Schreib-Tool nur zum Testen des Freigabe-Flows."""

from app.db.models import Notiz
from app.tools.base import BasisTool, ToolFehler, aktueller_nutzer

MAX_NOTIZ_ZEICHEN = 1000


class DemoNotiz(BasisTool):
    name = "demo_notiz"
    beschreibung = (
        "Speichert eine kurze Textnotiz in der internen Datenbank. Schreibendes Tool: Es wird "
        "erst ausgeführt, nachdem der Nutzer die Freigabe per Button erteilt hat. Nutze es nur, "
        "wenn der Nutzer ausdrücklich eine Notiz speichern möchte."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Inhalt der Notiz, max. 1000 Zeichen"},
        },
        "required": ["text"],
    }
    schreibend = True

    async def ausfuehren(self, text: str) -> dict:
        text = _pruefe(text)
        async with self.kontext.session_fabrik() as session:
            notiz = Notiz(user_id=aktueller_nutzer.get().id, text=text)
            session.add(notiz)
            await session.commit()
        return {"gespeichert": True, "notiz_id": notiz.id}

    def vorschau(self, text: str) -> str:
        return f"Notiz speichern: „{_pruefe(text)}“"


def _pruefe(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ToolFehler("Die Notiz ist leer.")
    if len(text) > MAX_NOTIZ_ZEICHEN:
        raise ToolFehler(f"Die Notiz ist länger als {MAX_NOTIZ_ZEICHEN} Zeichen.")
    return text.strip()
