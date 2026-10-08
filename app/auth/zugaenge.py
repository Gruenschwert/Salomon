"""Persönliche Zugänge zu Diensten: verbinden, trennen, auflisten, Übernahme des Bestands."""

import logging

from app.auth.kontext import NutzerKontext
from app.auth.tresor import (
    Tresor,
    TresorFehler,
    loesche_geheimnis,
    speichere_geheimnis,
    verbundene_dienste,
)
from app.auth.users import finde_erlaubten_nutzer, merker_gesetzt, setze_merker
from app.tools.asana_client import DIENST as ASANA
from app.tools.asana_client import erster_admin, pruefe_token
from app.tools.base import ToolFehler, ToolKontext

log = logging.getLogger(__name__)

# Dienst -> Erklärung, was die Person eingeben soll
DIENSTE = {
    ASANA: (
        "deinen persönlichen Asana-Zugriffstoken (Asana → Profilbild → Einstellungen → Apps → "
        "Entwicklerkonsole → „Neues Zugriffstoken“)"
    ),
}
MERKER_ASANA = "asana_token_uebernommen"
KEIN_SCHLUESSEL_TEXT = (
    "/verbinden ist deaktiviert: Auf dem Server fehlt SECRETS_MASTER_KEY. Bitte sprich den "
    "Admin an."
)


class Zugaenge:
    def __init__(self, kontext: ToolKontext) -> None:
        self._kontext = kontext
        self._session_fabrik = kontext.session_fabrik

    def _tresor(self) -> Tresor:
        try:
            tresor = Tresor.aus_settings(self._kontext.settings)
        except TresorFehler as exc:
            raise ToolFehler(f"/verbinden ist deaktiviert: {exc}") from None
        if not tresor.verfuegbar:
            raise ToolFehler(KEIN_SCHLUESSEL_TEXT)
        return tresor

    def pruefe_verfuegbar(self) -> None:
        """Wirft einen ToolFehler mit klarer Meldung, wenn kein Hauptschlüssel hinterlegt ist."""
        self._tresor()

    async def verbinde(self, nutzer: NutzerKontext, dienst: str, geheimnis: str) -> str:
        """Prüft die Zugangsdaten beim Dienst, speichert sie verschlüsselt und liefert den
        Namen des Kontos. Das Geheimnis verlässt diese Funktion nur verschlüsselt."""
        tresor = self._tresor()
        if dienst != ASANA:
            raise ToolFehler(f"Unbekannter Dienst. Möglich: {', '.join(DIENSTE)}.")
        geheimnis = geheimnis.strip()
        if not geheimnis or any(zeichen.isspace() for zeichen in geheimnis):
            raise ToolFehler("Das sieht nicht nach einem Token aus (leer oder mit Leerzeichen).")
        konto = await pruefe_token(self._kontext, geheimnis)
        await speichere_geheimnis(self._session_fabrik, tresor, nutzer, dienst, geheimnis)
        return konto

    async def trenne(self, nutzer: NutzerKontext, dienst: str) -> bool:
        return await loesche_geheimnis(self._session_fabrik, nutzer, dienst)

    async def liste(self, nutzer: NutzerKontext) -> list[str]:
        return await verbundene_dienste(self._session_fabrik, nutzer)


async def uebernehme_asana_token(kontext: ToolKontext) -> bool:
    """Ordnet den bisherigen ASANA_TOKEN aus der .env einmalig dem Konto des Admins zu.

    Läuft bei jedem Start und ist idempotent: Ein Merker verhindert, dass der Token nach einem
    späteren /trennen wieder auftaucht. Ohne SECRETS_MASTER_KEY passiert nichts; der Admin
    arbeitet dann weiter mit dem Token aus der .env, bis der Schlüssel gesetzt ist.
    """
    settings = kontext.settings
    token = settings.asana_token.get_secret_value().strip()
    admin_id = erster_admin(settings)
    if not token or admin_id is None:
        return False
    try:
        tresor = Tresor.aus_settings(settings)
    except TresorFehler:
        log.error("SECRETS_MASTER_KEY ist ungültig; der Asana-Token wurde nicht übernommen")
        return False
    if not tresor.verfuegbar or await merker_gesetzt(kontext.session_fabrik, MERKER_ASANA):
        return False
    admin = await finde_erlaubten_nutzer(kontext.session_fabrik, admin_id)
    if admin is None:
        return False
    uebernommen = ASANA not in await verbundene_dienste(kontext.session_fabrik, admin)
    if uebernommen:
        await speichere_geheimnis(kontext.session_fabrik, tresor, admin, ASANA, token)
    await setze_merker(kontext.session_fabrik, MERKER_ASANA)
    log.info("Asana-Token aus der .env dem Admin-Konto zugeordnet: %s", uebernommen)
    return uebernommen
