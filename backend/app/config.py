from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(repr=False)
    redis_url: str = Field(repr=False)
    bot_api_key: SecretStr
    bot_identity: str = Field(default="omni-task", min_length=1, max_length=100)
    dashboard_origin: str = "http://127.0.0.1:8080"
    app_env: Literal["development", "production", "test"] = "development"
    cookie_secure: bool = False
    session_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)
    login_ttl_seconds: int = Field(default=300, ge=1, le=900)
    cookie_name: str = "omni_session"
    bot_base_url: str = "http://bot:8001"
    voice_dispatch_enabled: bool = True
    live_enabled: bool = True
    live_channel: str = Field(default="omni-task:live:v1", min_length=1, max_length=200)
    live_heartbeat_seconds: float = Field(default=15, gt=0, le=15)
    live_dispatch_interval_seconds: float = Field(default=0.25, ge=0.05, le=5)
    voice_max_duration_seconds: int = Field(default=600, ge=1, le=600)
    voice_max_bytes: int = Field(default=19_000_000, ge=1, le=19_000_000)
    voice_download_timeout_seconds: float = Field(default=30, gt=0, le=30)
    voice_conversion_timeout_seconds: float = Field(default=30, gt=0, le=30)
    voice_provider_timeout_seconds: float = Field(default=90, gt=0, le=90)
    # Notification jobs have a 60-second hard limit; even the minimum lease must outlive it.
    voice_lease_seconds: int = Field(default=240, ge=90, le=600)
    voice_max_attempts: int = Field(default=3, ge=1, le=10)
    notification_max_attempts: int = Field(default=5, ge=1, le=10)
    voice_retry_base_seconds: float = Field(default=2, ge=0, le=60)
    voice_redispatch_seconds: float = Field(default=30, ge=1, le=300)
    voice_temp_dir: str = "/tmp/omni-voice"
    transcription_provider: Literal["disabled", "fake", "openai"] = "disabled"
    transcription_model: str = Field(default="whisper-1", min_length=1, max_length=100)
    openai_api_key: SecretStr | None = None
    allow_paid_transcription: bool = False
    fake_transcript: str = Field(
        default="[Demo transcription] Prepare the internship demonstration.", repr=False
    )

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def empty_openai_key(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_security(self):
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL with psycopg")
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use Redis")
        if len(self.bot_api_key.get_secret_value()) < 32:
            raise ValueError("BOT_API_KEY must have at least 32 characters")
        origin = urlsplit(self.dashboard_origin)
        bot_origin = urlsplit(self.bot_base_url)
        if (
            bot_origin.scheme not in {"http", "https"}
            or not bot_origin.hostname
            or bot_origin.username
            or bot_origin.password
            or bot_origin.path not in {"", "/"}
            or bot_origin.query
            or bot_origin.fragment
        ):
            raise ValueError("BOT_BASE_URL must be an HTTP(S) service origin")
        self.bot_base_url = self.bot_base_url.rstrip("/")
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("DASHBOARD_ORIGIN must be an HTTP(S) origin")
        self.dashboard_origin = self.dashboard_origin.rstrip("/")
        if self.app_env == "production" and (not self.cookie_secure or origin.scheme != "https"):
            raise ValueError("Production requires HTTPS and secure cookies")
        if (
            not self.cookie_secure
            and self.app_env != "test"
            and origin.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Insecure cookies are allowed only for loopback development")
        return self
