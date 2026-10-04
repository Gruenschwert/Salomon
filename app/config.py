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
    model_default: str
    model_cheap: str
    max_tool_iterations: int = 8
    max_output_tokens: int = 1500
    daily_cost_limit_eur: Decimal = Decimal("5")
    history_max_messages: int = 20

    # Kostenberechnung: Preise von MODEL_DEFAULT in USD pro 1 Mio. Tokens, fester Kurs
    price_input_usd_per_mtok: Decimal
    price_output_usd_per_mtok: Decimal
    usd_eur_rate: Decimal

    # Datenbank
    database_url: SecretStr

    # Shopify
    shopify_canasups_domain: str = ""
    shopify_canasups_token: SecretStr = SecretStr("")
    shopify_kiffkraut_domain: str = ""
    shopify_kiffkraut_token: SecretStr = SecretStr("")
    shopify_api_version: str = ""

    tz: str = "Europe/Berlin"

    @field_validator("telegram_allowed_user_ids", "telegram_admin_user_ids", mode="before")
    @classmethod
    def _ids_aus_kommaliste(cls, wert: object) -> object:
        if isinstance(wert, str):
            return frozenset(int(teil) for teil in wert.split(",") if teil.strip())
        return wert

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
