"""Settings (pydantic-settings). Secrets kommen ausschließlich aus der Umgebung bzw. `.env`."""

from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

IdListe = Annotated[frozenset[int], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    telegram_bot_token: SecretStr
    telegram_allowed_user_ids: IdListe = frozenset()
    telegram_admin_user_ids: IdListe = frozenset()

    # Anthropic
    anthropic_api_key: SecretStr
    # Optional: wird als Header `anthropic-workspace-id` mitgeschickt
    anthropic_workspace_id: str = ""
    # Modellstufen. MODEL_DEFAULT ist der frühere Name von MODEL_STANDARD und gilt weiter,
    # solange MODEL_STANDARD leer ist. MODEL_CHEAP wird nicht mehr gelesen.
    model_default: str = ""
    model_cheap: str = ""
    model_einfach: str = "claude-haiku-4-5-20251001"
    model_standard: str = ""
    model_komplex: str = "claude-opus-5-5"
    # Höchstzahl der Runden (Claude-Aufrufe) je Nachricht; früher MAX_TOOL_ITERATIONS
    agent_max_rounds: int = 25
    max_output_tokens: int = 8000
    # Tageslimit je Person (überschreibbar mit /limit) und Gesamtlimit aller zusammen
    daily_cost_limit_eur: Decimal = Decimal("5")
    daily_cost_limit_total_eur: Decimal = Decimal("20")
    history_max_messages: int = 20
    # Nachrichten werden nach so vielen Tagen gelöscht (0 = nie)
    message_retention_days: int = 90

    # Kostenberechnung: Preise von MODEL_DEFAULT in USD pro 1 Mio. Tokens, fester Kurs
    price_input_usd_per_mtok: Decimal
    price_output_usd_per_mtok: Decimal
    usd_eur_rate: Decimal

    # Datenbank
    database_url: SecretStr
    # Optional: eigener Datenbank-Login der Rolle app_laufzeit für den laufenden Bot. Leer:
    # Der Bot verbindet sich mit DATABASE_URL und wechselt beim Verbinden in app_laufzeit.
    app_database_url: SecretStr = SecretStr("")

    # Shopify
    shopify_canasups_domain: str = ""
    shopify_canasups_token: SecretStr = SecretStr("")
    shopify_kiffkraut_domain: str = ""
    shopify_kiffkraut_token: SecretStr = SecretStr("")
    shopify_api_version: str = ""

    # Verschlüsselung der Zugangsdaten je Person (Base64, 32 Byte). Leer: /verbinden ist aus.
    secrets_master_key: SecretStr = SecretStr("")
    secrets_master_key_version: int = 1
    # Nur während einer Rotation: der vorherige Schlüssel
    secrets_master_key_alt: SecretStr = SecretStr("")

    # Asana. ASANA_TOKEN dient nur noch der einmaligen Übernahme in das Konto des Admins.
    asana_token: SecretStr = SecretStr("")
    asana_workspace_gid: str = ""
    asana_default_team_gid: str = ""
    asana_max_ops_per_changeset: int = 100
    asana_max_deletes_per_changeset: int = 20
    asana_delete_enabled: bool = True
    asana_attachment_view_max_mb: float = 5
    asana_api_aufruf_enabled: bool = True

    # Mail (IMAP/SMTP). Die Server sind die von united-domains und gelten für jedes Postfach,
    # das beim Verbinden keine eigenen nennt.
    mail_imap_host: str = "imaps.udag.de"
    mail_imap_port: int = 993
    mail_smtp_host: str = "smtps.udag.de"
    mail_smtp_port: int = 465
    # Obergrenze für den Text einer Mail, der an das Modell geht
    mail_max_zeichen: int = 12000
    mail_anhang_max_mb: float = 5
    mail_max_senden_pro_tag: int = 20
    mail_max_empfaenger: int = 10
    # So lange bleibt gelesener Mailinhalt verschlüsselt im Gesprächsverlauf
    mail_kontext_ttl_stunden: int = 24
    mail_max_anmeldungen_pro_minute: int = 6

    # Foto-Eingang
    photo_max_mb: float = 5

    tz: str = "Europe/Berlin"

    @field_validator("telegram_allowed_user_ids", "telegram_admin_user_ids", mode="before")
    @classmethod
    def _ids_aus_kommaliste(cls, wert: object) -> object:
        if isinstance(wert, str):
            return frozenset(int(teil) for teil in wert.split(",") if teil.strip())
        return wert

    @model_validator(mode="before")
    @classmethod
    def _leere_werte_sind_standard(cls, werte: object) -> object:
        """Eine leere Zeile wie `ASANA_MAX_OPS_PER_CHANGESET=` bedeutet: Standardwert."""
        if isinstance(werte, dict):
            mit_standard = {
                name for name, feld in cls.model_fields.items() if not feld.is_required()
            }
            return {
                name: wert
                for name, wert in werte.items()
                if not (wert == "" and name.lower() in mit_standard)
            }
        return werte

    @model_validator(mode="after")
    def _standardmodell(self) -> "Settings":
        self.model_standard = (
            self.model_standard.strip() or self.model_default.strip() or "claude-sonnet-5-5"
        )
        return self

    def modell(self, stufe: str) -> str:
        """Die Modell-ID einer Stufe (einfach, standard, komplex)."""
        return {
            "einfach": self.model_einfach.strip() or self.model_standard,
            "komplex": self.model_komplex.strip() or self.model_standard,
        }.get(stufe, self.model_standard)

    @model_validator(mode="after")
    def _admins_sind_erlaubt(self) -> "Settings":
        if not self.telegram_admin_user_ids <= self.telegram_allowed_user_ids:
            raise ValueError(
                "TELEGRAM_ADMIN_USER_IDS muss eine Teilmenge von TELEGRAM_ALLOWED_USER_IDS sein"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
