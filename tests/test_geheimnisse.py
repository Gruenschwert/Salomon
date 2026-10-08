import logging

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError

from app.channels.base import EingehendeNachricht
from app.db.models import Message
from app.db.session import db_sitzung
from app.main import erstelle_anthropic_client
from app.observability.geheimnisse import (
    LEISE_LOGGER,
    MaskierenderFormatter,
    maskiere,
    richte_logging_ein,
    sammle_geheimnisse,
)
from tests.conftest import ERLAUBT_ID
from tests.fakes import FakeAnthropic, claude_antwort, text_block, tool_use_block


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


# ---------------------------------------------------------------- Inhalte bleiben aus den Logs


def test_adresse_der_telegram_api_wird_maskiert(settings):
    """Die Adresse jeder Telegram-Anfrage enthält den Bot-Token."""
    formatter = MaskierenderFormatter(sammle_geheimnisse(_settings(settings)))
    record = logging.LogRecord(
        "httpx",
        logging.WARNING,
        __file__,
        1,
        "HTTP Request: POST %s",
        ("https://api.telegram.org/bot123456:telegram-geheim/sendMessage",),
        None,
    )
    zeile = formatter.format(record)
    assert "https://api.telegram.org/bot***/sendMessage" in zeile
    assert "telegram-geheim" not in zeile


def test_hauptschluessel_gehoert_zu_den_maskierten_werten(settings):
    geheimnisse = sammle_geheimnisse(settings)
    assert settings.secrets_master_key.get_secret_value() in geheimnisse


async def test_gespraech_und_tool_daten_stehen_nicht_im_log(baue_agent, user, caplog):
    """Ein ganzer Durchlauf mit Tool-Aufruf auf DEBUG: weder Frage noch Tool-Eingabe noch
    Antwort landen im Log."""
    client = FakeAnthropic(
        claude_antwort(tool_use_block("beispiel_lesen", {"text": "TOOL-EINGABE-4711"})),
        claude_antwort(tool_use_block("beispiel_kaputt", {})),
        claude_antwort(text_block("ANTWORT-4711")),
    )
    nachricht = EingehendeNachricht(
        chat_id=1, absender_id=ERLAUBT_ID, absender_name="X", text="Gehalt FRAGE-4711"
    )
    with caplog.at_level(logging.DEBUG):
        antwort = await baue_agent(client).beantworte(nachricht, user)
    assert antwort.text == "ANTWORT-4711"
    # Der Fehler im Tool wird protokolliert, die Inhalte nicht.
    assert "Unerwarteter Fehler im Tool beispiel_kaputt" in caplog.text
    assert "4711" not in caplog.text


async def test_datenbankfehler_nennen_keine_inhalte(laufzeit_fabrik, user):
    """Scheitert das Speichern einer Nachricht, steht ihr Text nicht in der Fehlermeldung."""
    with pytest.raises(DBAPIError) as fehler:
        async with db_sitzung(laufzeit_fabrik, user) as session:
            # chat_id fehlt: die Datenbank lehnt die Zeile ab
            session.add(Message(user_id=user.id, rolle="user", inhalt="GEHEIMER-TEXT-4711"))
            await session.commit()
    assert "4711" not in str(fehler.value)
    assert "hidden" in str(fehler.value)
