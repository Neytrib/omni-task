"""Bot-only configuration: never read database, Redis, or provider credentials."""

from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    api_base_url: str = "http://api:8000"
    bot_api_key: SecretStr
    telegram_bot_token: SecretStr | None = None
    voice_max_duration_seconds: int = Field(default=600, ge=1, le=600)
    voice_download_timeout_seconds: int = Field(default=30, ge=1, le=120)
    voice_max_bytes: int = Field(default=19_000_000, ge=1, le=19_000_000)

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def empty_token_disables_polling(cls, value):
        if value is None or value == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_service_configuration(self):
        parsed = urlsplit(self.api_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("API_BASE_URL must be an HTTP(S) service origin")
        self.api_base_url = self.api_base_url.rstrip("/")
        if len(self.bot_api_key.get_secret_value()) < 32:
            raise ValueError("BOT_API_KEY must have at least 32 characters")
        if self.telegram_bot_token is not None:
            try:
                validate_token(self.telegram_bot_token.get_secret_value())
            except TokenValidationError:
                raise ValueError("TELEGRAM_BOT_TOKEN has an invalid format") from None
        return self
