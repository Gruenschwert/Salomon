"""Startpunkt: DB-Check, Bot starten."""

import logging

import anthropic

from app import __version__
from app.agent.loop import Agent
from app.auth.approvals import Freigaben
from app.auth.users import uebernehme_bestand
from app.channels.telegram import TelegramKanal
from app.config import Settings, get_settings
from app.db.session import db_check, erstelle_engine, erstelle_session_fabrik
from app.observability.alerts import Alarme
from app.observability.costs import Kosten
from app.observability.geheimnisse import richte_logging_ein
from app.tools.base import ToolKontext
from app.tools.registry import lade_registry

log = logging.getLogger(__name__)

# Darunter passen größere Änderungssätze nicht in eine Antwort von Claude.
MIN_OUTPUT_TOKENS_EMPFOHLEN = 4000


def erstelle_anthropic_client(settings: Settings) -> anthropic.AsyncAnthropic:
    kopfzeilen = {}
    if workspace_id := settings.anthropic_workspace_id.strip():
        kopfzeilen["anthropic-workspace-id"] = workspace_id
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), default_headers=kopfzeilen or None
    )


def main() -> None:
    settings = get_settings()
    richte_logging_ein(settings)
    laufzeit_url = settings.app_database_url.get_secret_value().strip()
    engine = erstelle_engine(laufzeit_url or settings.database_url.get_secret_value())
    session_fabrik = erstelle_session_fabrik(engine)

    client = erstelle_anthropic_client(settings)
    kontext = ToolKontext(settings=settings, session_fabrik=session_fabrik)
    registry = lade_registry(kontext)
    alarme = Alarme(session_fabrik)
    kosten = Kosten(settings, session_fabrik, alarme)
    freigaben = Freigaben(kontext, registry)
    agent = Agent(settings, session_fabrik, client, registry, freigaben, kosten)

    async def beim_start() -> None:
        await db_check(engine)
        await uebernehme_bestand(session_fabrik, settings)
        log.info("gs-assistant %s gestartet", __version__)
        await alarme.melde(f"✅ gs-assistant {__version__} wurde gestartet.")
        if settings.max_output_tokens < MIN_OUTPUT_TOKENS_EMPFOHLEN:
            await alarme.melde(
                f"⚠️ MAX_OUTPUT_TOKENS={settings.max_output_tokens} ist niedrig. Lange Antworten "
                "und große Asana-Änderungssätze werden damit abgeschnitten. Empfohlen: 8000."
            )

    kanal = TelegramKanal(
        settings,
        session_fabrik,
        handler=agent.beantworte,
        freigaben=freigaben,
        kosten=kosten,
        alarme=alarme,
        beim_start=beim_start,
    )
    alarme.verbinde(kanal.sende_antwort)
    kontext.dateien.verbinde(kanal.lade_datei)
    kanal.starte()


if __name__ == "__main__":
    main()
