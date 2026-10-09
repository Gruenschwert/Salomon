"""Gemeinsame Bausteine der Mail-Tests: verbundene Postfächer und Tool-Aufrufe."""

from app.auth.tresor import Tresor
from app.mail.konten import Postfach, speichere_postfach
from app.tools.registry import ToolErgebnis, fuehre_tool_aus, lade_registry

ADRESSE_A = "lea@gruenschwert.example"
PASSWORT_A = "Sehr-Geheimes-Passwort-von-Lea-123"
ADRESSE_SHOP = "shop@gruenschwert.example"
PASSWORT_SHOP = "Shop-Passwort-geheim-789"
ADRESSE_B = "theis@gruenschwert.example"
PASSWORT_B = "Anderes-Passwort-von-Theis-456"
ALLE_PASSWOERTER = (PASSWORT_A, PASSWORT_SHOP, PASSWORT_B)


async def verbinde(kontext, server, nutzer, label: str, adresse: str, passwort: str, **extra):
    """Legt das Postfach auf dem Test-Server an und verbindet es für die Person."""
    if adresse not in server.postfaecher:
        server.postfach(adresse, passwort, **extra)
    postfach = Postfach(label, adresse, passwort, "imaps.udag.de", 993, "smtps.udag.de", 465)
    tresor = Tresor.aus_settings(kontext.settings)
    await speichere_postfach(kontext.session_fabrik, tresor, nutzer, postfach)
    return server.postfaecher[adresse]


class Werkzeuge:
    """Ruft die echten Mail-Tools so auf, wie es der Agent tut."""

    def __init__(self, kontext) -> None:
        self.kontext = kontext
        self.registry = lade_registry(kontext)

    async def rufe(self, name: str, nutzer, **params) -> ToolErgebnis:
        return await fuehre_tool_aus(self.registry.hole(name), params, nutzer, self.kontext)

    async def daten(self, name: str, nutzer, **params) -> dict:
        ergebnis = await self.rufe(name, nutzer, **params)
        assert not ergebnis.fehler, ergebnis.text
        return ergebnis.daten
