import logging

from pydantic import SecretStr

from app.main import erstelle_anthropic_client
from app.observability.geheimnisse import (
    LEISE_LOGGER,
    MaskierenderFormatter,
    maskiere,
    richte_logging_ein,
    sammle_geheimnisse,
)


def _settings(settings):
    return settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("123456:telegram-geheim"),
            "anthropic_api_key": SecretStr("sk-ant-geheim"),
            "asana_token": SecretStr("asana-geheim"),
            "database_url": SecretStr("postgresql+asyncpg://gs:dbpasswort@db:5432/gs"),
        }
    )


def test_alle_secrets_werden_gesammelt(settings):
    geheimnisse = sammle_geheimnisse(_settings(settings))
    for wert in ("123456:telegram-geheim", "sk-ant-geheim", "asana-geheim", "dbpasswort"):
        assert wert in geheimnisse
    assert "" not in geheimnisse


def test_maskiere(settings):
    geheimnisse = sammle_geheimnisse(_settings(settings))
    text = maskiere("GET https://api.telegram.org/bot123456:telegram-geheim/getMe", geheimnisse)
    assert text == "GET https://api.telegram.org/bot***/getMe"


def test_formatter_maskiert_nachricht_argumente_und_traceback(settings):
    formatter = MaskierenderFormatter(sammle_geheimnisse(_settings(settings)))
    try:
        raise RuntimeError("Key sk-ant-geheim abgelehnt")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "Fehler bei %s", ("asana-geheim",), sys.exc_info()
        )
    zeile = formatter.format(record)
    assert "Fehler bei ***" in zeile
    assert "RuntimeError: Key *** abgelehnt" in zeile
    assert "-geheim" not in zeile


def test_logging_einrichtung_maskiert_und_stellt_bibliotheken_leise(settings, capsys):
    wurzel = logging.getLogger()
    vorher = (wurzel.handlers[:], wurzel.level)
    try:
        richte_logging_ein(_settings(settings))
        logging.getLogger("app.test").info("Token asana-geheim und Key sk-ant-geheim")
        for name in LEISE_LOGGER:
            assert logging.getLogger(name).level == logging.WARNING
        assert {"httpx", "telegram"} <= set(LEISE_LOGGER)
    finally:
        wurzel.handlers[:], wurzel.level = vorher[0], vorher[1]
    ausgabe = capsys.readouterr().err
    assert "Token *** und Key ***" in ausgabe
    assert "-geheim" not in ausgabe


def test_anthropic_client_ohne_workspace_id(settings):
    client = erstelle_anthropic_client(settings)
    assert "anthropic-workspace-id" not in client.default_headers


def test_anthropic_client_mit_workspace_id(settings):
    client = erstelle_anthropic_client(
        settings.model_copy(update={"anthropic_workspace_id": " wrkspc_01 "})
    )
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_01"
