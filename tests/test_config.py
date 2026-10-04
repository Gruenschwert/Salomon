import pytest
from pydantic import ValidationError

from app.config import Settings

PFLICHT = {
    "TELEGRAM_BOT_TOKEN": "geheim-token",
    "ANTHROPIC_API_KEY": "geheim-key",
    "MODEL_DEFAULT": "modell-a",
    "MODEL_CHEAP": "modell-b",
    "DATABASE_URL": "sqlite+aiosqlite://",
}


def _setze(monkeypatch, **extra):
    for name, wert in {**PFLICHT, **extra}.items():
        monkeypatch.setenv(name, wert)


def test_ids_aus_kommaliste(monkeypatch):
    _setze(monkeypatch, TELEGRAM_ALLOWED_USER_IDS="1, 2,3", TELEGRAM_ADMIN_USER_IDS="2")
    settings = Settings(_env_file=None)
    assert settings.telegram_allowed_user_ids == {1, 2, 3}
    assert settings.telegram_admin_user_ids == {2}


def test_leere_id_liste(monkeypatch):
    _setze(monkeypatch, TELEGRAM_ALLOWED_USER_IDS="")
    assert Settings(_env_file=None).telegram_allowed_user_ids == frozenset()


def test_admin_muss_erlaubt_sein(monkeypatch):
    _setze(monkeypatch, TELEGRAM_ALLOWED_USER_IDS="1", TELEGRAM_ADMIN_USER_IDS="2")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secrets_erscheinen_nicht_in_repr(monkeypatch):
    _setze(monkeypatch)
    text = repr(Settings(_env_file=None))
    assert "geheim-token" not in text
    assert "geheim-key" not in text


def test_standardwerte(monkeypatch):
    _setze(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.max_tool_iterations == 8
    assert settings.history_max_messages == 20
    assert settings.tz == "Europe/Berlin"
