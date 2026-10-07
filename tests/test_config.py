import pytest
from pydantic import ValidationError

from app.config import Settings

PFLICHT = {
    "TELEGRAM_BOT_TOKEN": "geheim-token",
    "ANTHROPIC_API_KEY": "geheim-key",
    "MODEL_DEFAULT": "modell-a",
    "MODEL_CHEAP": "modell-b",
    "DATABASE_URL": "sqlite+aiosqlite://",
    "PRICE_INPUT_USD_PER_MTOK": "2.00",
    "PRICE_OUTPUT_USD_PER_MTOK": "10.00",
    "USD_EUR_RATE": "0.9",
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


def test_asana_standardwerte(monkeypatch):
    _setze(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.asana_token.get_secret_value() == ""
    assert settings.asana_workspace_gid == ""
    assert settings.asana_max_ops_per_changeset == 100
    assert settings.asana_max_deletes_per_changeset == 20
    assert settings.asana_delete_enabled is True
    assert settings.asana_delete_roles == {"admin"}
    assert settings.photo_max_mb == 5
    assert settings.anthropic_workspace_id == ""


def test_asana_werte_aus_umgebung(monkeypatch):
    _setze(
        monkeypatch,
        ASANA_TOKEN="asana-geheim",
        ASANA_DELETE_ROLES="admin, user",
        ASANA_DELETE_ENABLED="false",
        ASANA_MAX_OPS_PER_CHANGESET="10",
    )
    settings = Settings(_env_file=None)
    assert settings.asana_delete_roles == {"admin", "user"}
    assert settings.asana_delete_enabled is False
    assert settings.asana_max_ops_per_changeset == 10
    assert "asana-geheim" not in repr(settings)


def test_env_example_hat_keine_kommentare_hinter_werten():
    from pathlib import Path

    zeilen = (Path(__file__).parent.parent / ".env.example").read_text().splitlines()
    for zeile in zeilen:
        if zeile and not zeile.startswith("#"):
            assert "#" not in zeile, zeile
    for name in ("ASANA_TOKEN=", "ANTHROPIC_API_KEY=", "TELEGRAM_BOT_TOKEN=", "POSTGRES_PASSWORD="):
        assert name in zeilen
