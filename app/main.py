"""Startpunkt: DB-Check, Bot starten."""

import logging

import anthropic

from app import __version__
from app.agent.loop import Agent
from app.auth.users import synchronisiere_whitelist
from app.channels.telegram import TelegramKanal
from app.config import get_settings
from app.db.session import db_check, erstelle_engine, erstelle_session_fabrik
from app.tools.base import ToolKontext
from app.tools.registry import lade_registry

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx protokolliert auf INFO jede URL – die der Telegram-API enthält den Bot-Token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    engine = erstelle_engine(settings.database_url.get_secret_value())
    session_fabrik = erstelle_session_fabrik(engine)

    async def beim_start() -> None:
        await db_check(engine)
        await synchronisiere_whitelist(session_fabrik, settings)
        log.info("gs-assistant %s gestartet", __version__)

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    registry = lade_registry(ToolKontext(settings=settings, session_fabrik=session_fabrik))
    agent = Agent(settings, session_fabrik, client, registry)
    kanal = TelegramKanal(settings, session_fabrik, handler=agent.beantworte, beim_start=beim_start)
    kanal.starte()


if __name__ == "__main__":
    main()
