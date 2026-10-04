"""Startpunkt: DB-Check, Bot starten."""

import logging

from app import __version__
from app.auth.users import synchronisiere_whitelist
from app.channels.base import Antwort, EingehendeNachricht
from app.channels.telegram import TelegramKanal
from app.config import get_settings
from app.db.models import User
from app.db.session import db_check, erstelle_engine, erstelle_session_fabrik

log = logging.getLogger(__name__)


async def echo(nachricht: EingehendeNachricht, user: User) -> Antwort:
    return Antwort(text=nachricht.text)


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

    kanal = TelegramKanal(settings, session_fabrik, handler=echo, beim_start=beim_start)
    kanal.starte()


if __name__ == "__main__":
    main()
