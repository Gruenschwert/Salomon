from app.tools.base import BasisTool


class BeispielSchreiben(BasisTool):
    name = "beispiel_schreiben"
    beschreibung = "Schreibt etwas (nur nach Freigabe)."
    parameter_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    schreibend = True

    ausgefuehrt: list[str] = []

    async def ausfuehren(self, text: str) -> dict:
        self.ausgefuehrt.append(text)
        return {"gespeichert": text}

    def vorschau(self, text: str) -> str:
        return f"Schreiben: {text}"
