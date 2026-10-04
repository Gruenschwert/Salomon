"""Startpunkt: DB-Check, Bot starten."""

import logging

from app import __version__

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log.info("gs-assistant %s gestartet", __version__)


if __name__ == "__main__":
    main()
