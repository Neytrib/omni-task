"""Provider contracts are verified with synthetic files and an in-process HTTP transport."""

import asyncio
import json
import logging
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from app.voice_provider import (
    MAX_PROVIDER_RESPONSE_BYTES,
    DisabledTranscriber,
    FakeTranscriber,
    OpenAITranscriber,
    VoiceFailure,
    build_provider,
    retry_after,
    validate_transcript,
)
from pydantic import SecretStr

SENTINEL = "SYNTHETIC_PRIVATE_PROVIDER_DATA_DO_NOT_LOG"


@pytest.fixture
def audio(tmp_path):
    file = tmp_path / "synthetic.wav"
    with wave.open(str(file), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    return file


def adapter(handler, **kwargs):
    return OpenAITranscriber(
        api_key=SecretStr(SENTINEL),
        allow_paid_calls=True,
        http_transport=httpx.MockTransport(handler),
        **kwargs,
    )


def assert_failure(provider, audio, code, retryable=False):
    with pytest.raises(VoiceFailure) as captured:
        asyncio.run(provider.transcribe(audio))
    assert captured.value.code == code
    assert captured.value.retryable is retryable
    assert SENTINEL not in str(captured.value)
    assert SENTINEL not in repr(captured.value)
    return captured.value


def test_provider_preserves_complete_content_and_sends_only_transcription_fields(audio, caplog):
    caplog.set_level(logging.DEBUG)
    original = "  First line\n" + "Հայերեն🙂 " * 4000 + "\nFinal line.  "
    requests = []

    async def receive(request):
        requests.append(request)
        body = await request.aread()
        assert request.url == "https://api.openai.com/v1/audio/transcriptions"
        assert request.method == "POST"
        assert request.headers["Authorization"] == f"Bearer {SENTINEL}"
        assert b'filename="speech.wav"' in body
        assert b"Content-Type: audio/wav" in body
        assert b'name="model"\r\n\r\nwhisper-1' in body
        assert b'name="response_format"\r\n\r\njson' in body
        assert b'name="prompt"' not in body
        return httpx.Response(200, json={"text": original})

    provider = adapter(receive)
    assert asyncio.run(provider.transcribe(audio)) == original
    assert len(requests) == 1
    assert SENTINEL not in repr(provider)
    assert SENTINEL not in caplog.text
    assert original not in caplog.text


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (429, "provider_rate_limited", True),
        (408, "provider_unavailable", True),
        (425, "provider_unavailable", True),
        (500, "provider_unavailable", True),
        (503, "provider_unavailable", True),
        (401, "provider_authentication", False),
        (403, "provider_authentication", False),
        (400, "provider_rejected", False),
        (404, "provider_rejected", False),
        (302, "provider_rejected", False),
    ],
)
def test_provider_classifies_failures_without_raw_errors_or_automatic_retry(
    audio, caplog, status, code, retryable
):
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(
            status,
            text=SENTINEL,
            headers={"Retry-After": "999999", "Location": "https://invalid.example/secret"},
        )

    failure = assert_failure(adapter(receive), audio, code, retryable)
    assert failure.retry_after_seconds == (300 if status == 429 else None)
    assert len(calls) == 1
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (b"not JSON", "provider_invalid_response"),
        (json.dumps([]).encode(), "provider_invalid_response"),
        (json.dumps({}).encode(), "provider_invalid_response"),
        (json.dumps({"text": 42}).encode(), "provider_invalid_response"),
        (json.dumps({"text": " \n\t"}).encode(), "empty_transcript"),
        (json.dumps({"text": "a" * 50_001}).encode(), "transcript_too_large"),
        (json.dumps({"text": "before\x00after"}).encode(), "invalid_transcript"),
        (json.dumps({"text": "before\ud800after"}).encode(), "invalid_transcript"),
    ],
)
def test_provider_rejects_unusable_transcripts_without_creating_replacement_text(
    audio, response, code
):
    assert_failure(adapter(lambda request: httpx.Response(200, content=response)), audio, code)


def test_provider_caps_streamed_response_bytes(audio):
    class LargeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(MAX_PROVIDER_RESPONSE_BYTES // 8192 + 1):
                yield b"x" * 8192

    provider = adapter(lambda request: httpx.Response(200, stream=LargeBody()))
    assert_failure(provider, audio, "provider_response_too_large")


@pytest.mark.parametrize(
    ("error_type", "code"),
    [(httpx.ReadTimeout, "provider_timeout"), (httpx.ConnectError, "provider_unavailable")],
)
def test_provider_transport_failures_are_safe_and_retryable(audio, error_type, code):
    def receive(request):
        raise error_type(SENTINEL, request=request)

    assert_failure(adapter(receive), audio, code, retryable=True)


def test_provider_total_timeout_bounds_slow_transport(audio):
    async def receive(request):
        await asyncio.sleep(5)
        raise AssertionError("Total deadline should cancel this request")

    assert_failure(
        adapter(receive, timeout_seconds=0.01), audio, "provider_timeout", retryable=True
    )


def test_provider_payment_gate_precedes_transport_and_file_access(audio):
    def forbidden(request):
        pytest.fail("A disabled paid provider must not make an HTTP request")

    provider = replace(adapter(forbidden), allow_paid_calls=False)
    assert_failure(provider, audio, "paid_transcription_not_enabled")
    provider = replace(provider, allow_paid_calls=True, api_key=SecretStr(""))
    assert_failure(provider, audio, "provider_not_configured")
    assert_failure(replace(provider, api_key=SecretStr("   ")), audio, "provider_not_configured")


def test_provider_rejects_missing_symlink_and_oversized_input_before_request(audio, tmp_path):
    def forbidden(request):
        pytest.fail("Invalid input must not reach the provider")

    provider = adapter(forbidden)
    assert_failure(provider, tmp_path / "missing", "invalid_audio")
    linked = tmp_path / "linked.wav"
    linked.symlink_to(audio)
    assert_failure(provider, linked, "invalid_audio")
    with audio.open("wb") as output:
        output.truncate(20_000_001)
    assert_failure(provider, audio, "audio_too_large")


def test_configured_provider_selection_has_no_implicit_fake_fallback(audio):
    settings = SimpleNamespace(
        transcription_provider="disabled",
        transcription_model="whisper-1",
        openai_api_key=None,
        allow_paid_transcription=False,
        voice_provider_timeout_seconds=90,
        fake_transcript="[Demo transcription]  Exact synthetic content.\n",
    )
    assert_failure(build_provider(settings), audio, "provider_not_configured")
    settings.transcription_provider = "openai"
    assert_failure(build_provider(settings), audio, "provider_not_configured")
    settings.openai_api_key = SecretStr(SENTINEL)
    assert_failure(build_provider(settings), audio, "paid_transcription_not_enabled")
    settings.allow_paid_transcription = True
    assert isinstance(build_provider(settings), OpenAITranscriber)
    settings.transcription_provider = "fake"
    provider = build_provider(settings)
    assert isinstance(provider, FakeTranscriber)
    assert asyncio.run(provider.transcribe(audio)) == settings.fake_transcript
    assert settings.fake_transcript not in repr(provider)
    assert isinstance(DisabledTranscriber(), DisabledTranscriber)


def test_transcript_limit_accepts_complete_boundary_without_stripping():
    original = "  " + "🙂" * 49_996 + "\n "
    assert len(original) == 50_000
    assert validate_transcript(original) is original


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "-inf", ""])
def test_unusable_retry_after_is_ignored(value):
    assert retry_after(value) is None


def test_retry_after_supports_bounded_seconds_and_http_dates():
    assert retry_after("-20") == 0
    assert retry_after("12.5") == 12.5
    assert retry_after("100000") == 300
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 118 <= retry_after(future) <= 120
    assert retry_after(None) is None


def test_failure_keeps_only_sanitized_code_and_bounded_delay():
    failure = VoiceFailure("Bearer private-data", True, 100000)
    assert str(failure) == "voice_failed"
    assert failure.retry_after_seconds == 300
    assert VoiceFailure("safe_code", True, float("nan")).retry_after_seconds is None
