"""Hält Secrets aus Logs und Fehlermeldungen heraus."""

import logging

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.config import Settings

MASKIERT = "***"
# Kürzere Werte werden nicht ersetzt, sonst würden zufällige Treffer die Logs unlesbar machen.
MIN_LAENGE = 6
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Diese Bibliotheken protokollieren auf INFO jede URL – die der Telegram-API enthält den Bot-Token.
LEISE_LOGGER = ("httpx", "httpx2", "httpcore", "telegram")


def sammle_geheimnisse(settings: Settings) -> tuple[str, ...]:
    """Alle Secret-Werte der Settings, dazu das Passwort aus der Datenbank-URL."""
    werte = set()
    for name in type(settings).model_fields:
        wert = getattr(settings, name)
        if isinstance(wert, SecretStr):
            werte.add(wert.get_secret_value().strip())
    try:
        werte.add(make_url(settings.database_url.get_secret_value()).password or "")
    except ArgumentError:
        pass
    # Längste zuerst, damit ein Secret, das ein anderes enthält, vollständig ersetzt wird.
    return tuple(sorted((w for w in werte if len(w) >= MIN_LAENGE), key=len, reverse=True))


def maskiere(text: str, geheimnisse: tuple[str, ...]) -> str:
    for geheimnis in geheimnisse:
        text = text.replace(geheimnis, MASKIERT)
    return text


class MaskierenderFormatter(logging.Formatter):
    """Ersetzt Secrets in der fertigen Log-Zeile, einschließlich Argumenten und Tracebacks."""

    def __init__(self, geheimnisse: tuple[str, ...], fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt)
        self._geheimnisse = geheimnisse

    def format(self, record: logging.LogRecord) -> str:
        return maskiere(super().format(record), self._geheimnisse)


def richte_logging_ein(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(MaskierenderFormatter(sammle_geheimnisse(settings)))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in LEISE_LOGGER:
        logging.getLogger(name).setLevel(logging.WARNING)
