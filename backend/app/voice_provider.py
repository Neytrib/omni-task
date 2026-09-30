"""Replaceable transcription adapters; failures never retain provider bodies or credentials."""

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import SecretStr

MAX_TRANSCRIPT_CHARACTERS = 50_000
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
MAX_PROVIDER_AUDIO_BYTES = 20_000_000


class VoiceFailure(Exception):
    """Only fixed operational codes may leave the media/provider boundary."""

    def __init__(
        self, code: str, retryable: bool, retry_after_seconds: float | None = None
    ) -> None:
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "voice_failed"
        self.retryable = retryable
        self.retry_after_seconds = (
            min(max(retry_after_seconds, 0.0), 300.0)
            if retry_after_seconds is not None and math.isfinite(retry_after_seconds)
            else None
        )
        super().__init__(self.code)


def validate_transcript(value: object) -> str:
    """Validate, but do not strip, normalize, rewrite, or truncate accepted content."""
    if not isinstance(value, str):
        raise VoiceFailure("provider_invalid_response", False)
    if not value.strip():
        raise VoiceFailure("empty_transcript", False)
    if len(value) > MAX_TRANSCRIPT_CHARACTERS:
        raise VoiceFailure("transcript_too_large", False)
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise VoiceFailure("invalid_transcript", False)
    return value


def retry_after(value: str | None) -> float | None:
    """Honor bounded Retry-After delays without keeping the original header."""
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            delay = (parsed - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(delay, 0.0), 300.0) if math.isfinite(delay) else None


class TranscriptionProvider(Protocol):
    async def transcribe(self, audio: Path) -> str: ...


@dataclass
class DisabledTranscriber:
    code: str = "provider_not_configured"

    async def transcribe(self, audio: Path) -> str:
        raise VoiceFailure(self.code, False)


@dataclass
class FakeTranscriber:
    """Explicit test/demo adapter. It never runs as a fallback for provider failure."""

    transcript: str = field(repr=False)

    async def transcribe(self, audio: Path) -> str:
        return validate_transcript(self.transcript)


@dataclass
class OpenAITranscriber:
    api_key: SecretStr = field(repr=False)
    model: str = "whisper-1"
    timeout_seconds: float = 90
    allow_paid_calls: bool = False
    http_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def transcribe(self, audio: Path) -> str:
        if not self.allow_paid_calls:
            raise VoiceFailure("paid_transcription_not_enabled", False)
        if not self.api_key.get_secret_value().strip():
            raise VoiceFailure("provider_not_configured", False)
        try:
            if audio.is_symlink() or not audio.is_file():
                raise VoiceFailure("invalid_audio", False)
            if not 0 < audio.stat().st_size <= MAX_PROVIDER_AUDIO_BYTES:
                raise VoiceFailure("audio_too_large", False)
            # A total deadline bounds slow response streams as well as individual socket reads.
            async with asyncio.timeout(self.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout_seconds, connect=10),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.http_transport,
                ) as client:
                    with audio.open("rb") as file:
                        async with client.stream(
                            "POST",
                            "https://api.openai.com/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}"},
                            data={"model": self.model, "response_format": "json"},
                            files={"file": ("speech.wav", file, "audio/wav")},
                        ) as response:
                            status = response.status_code
                            if status == 429:
                                raise VoiceFailure(
                                    "provider_rate_limited",
                                    True,
                                    retry_after(response.headers.get("retry-after")),
                                )
                            if status >= 500 or status in {408, 425}:
                                raise VoiceFailure("provider_unavailable", True)
                            if status in {401, 403}:
                                raise VoiceFailure("provider_authentication", False)
                            if status != 200:
                                raise VoiceFailure("provider_rejected", False)
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(body) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES:
                                    raise VoiceFailure("provider_response_too_large", False)
                                body.extend(chunk)
            try:
                document = json.loads(body)
            except (ValueError, UnicodeError):
                raise VoiceFailure("provider_invalid_response", False) from None
            if not isinstance(document, dict):
                raise VoiceFailure("provider_invalid_response", False)
            return validate_transcript(document.get("text"))
        except (TimeoutError, httpx.TimeoutException):
            raise VoiceFailure("provider_timeout", True) from None
        except httpx.HTTPError:
            raise VoiceFailure("provider_unavailable", True) from None
        except OSError:
            raise VoiceFailure("audio_unavailable", True) from None


class ProviderSettings(Protocol):
    transcription_provider: str
    transcription_model: str
    openai_api_key: SecretStr | None
    allow_paid_transcription: bool
    voice_provider_timeout_seconds: float
    fake_transcript: str


def build_provider(settings: ProviderSettings) -> TranscriptionProvider:
    if settings.transcription_provider == "fake":
        return FakeTranscriber(settings.fake_transcript)
    if settings.transcription_provider == "openai":
        if settings.openai_api_key is None:
            return DisabledTranscriber()
        if not settings.allow_paid_transcription:
            return DisabledTranscriber("paid_transcription_not_enabled")
        return OpenAITranscriber(
            api_key=settings.openai_api_key,
            model=settings.transcription_model,
            timeout_seconds=settings.voice_provider_timeout_seconds,
            allow_paid_calls=True,
        )
    return DisabledTranscriber()
