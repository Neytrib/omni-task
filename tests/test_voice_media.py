"""Bounded transport and real FFmpeg decoding use only synthetic local recordings."""

import asyncio
import json
import logging
import os
import shutil
import stat
import subprocess
import time
import wave
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from app.voice_media import (
    MediaLimits,
    audio_workspace,
    cleanup_stale_audio,
    download_audio,
    prepare_audio,
)
from app.voice_provider import VoiceFailure
from pydantic import SecretStr

KEY = "SYNTHETIC_PRIVATE_BOT_CREDENTIAL_DO_NOT_LOG"
LIMITS = MediaLimits()


def download(
    directory, handler, *, limits=LIMITS, request_id=None, lease=None, origin="http://bot:8001"
):
    return download_audio(
        request_id or uuid4(),
        lease or uuid4(),
        directory,
        bot_base_url=origin,
        bot_api_key=SecretStr(KEY),
        limits=limits,
        transport=httpx.MockTransport(handler),
    )


def expect_failure(awaitable, code, retryable=False):
    with pytest.raises(VoiceFailure) as captured:
        asyncio.run(awaitable)
    assert captured.value.code == code
    assert captured.value.retryable is retryable
    assert KEY not in str(captured.value)
    return captured.value


def test_download_authenticates_fixed_route_and_preserves_complete_file(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    identifier, lease = uuid4(), uuid4()
    complete = bytes(range(256)) * 100
    calls = []

    async def receive(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == f"http://bot:8001/internal/voice/{identifier}/audio"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert json.loads(await request.aread()) == {"lease_token": str(lease)}
        assert request.headers["X-Correlation-ID"] == str(identifier)
        return httpx.Response(200, content=complete)

    result = asyncio.run(download(tmp_path, receive, request_id=identifier, lease=lease))
    assert result.read_bytes() == complete
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert len(calls) == 1
    assert KEY not in caplog.text
    assert str(lease) not in caplog.text


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "bot_authentication", False),
        (403, "bot_authentication", False),
        (404, "audio_unavailable", False),
        (413, "audio_too_large", False),
        (429, "download_unavailable", True),
        (503, "download_unavailable", True),
        (302, "download_rejected", False),
    ],
)
def test_download_http_failures_do_not_leave_files_or_follow_redirects(
    tmp_path, caplog, status, code, retryable
):
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(
            status,
            text=KEY,
            headers={"Retry-After": "10000", "Location": "https://invalid.example/private"},
        )

    failure = expect_failure(download(tmp_path, receive), code, retryable)
    assert failure.retry_after_seconds == (300 if retryable else None)
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []
    assert KEY not in caplog.text


@pytest.mark.parametrize(
    ("header", "content", "code"),
    [
        ("10001", b"a", "audio_too_large"),
        ("-1", b"a", "audio_too_large"),
        ("unknown", b"a", "invalid_audio"),
        (None, b"", "empty_audio"),
        (None, b"a" * 10001, "audio_too_large"),
    ],
)
def test_download_rejects_declared_and_actual_size_violations(tmp_path, header, content, code):
    def receive(request):
        response = httpx.Response(200, content=content)
        response.headers.pop("Content-Length", None)
        if header is not None:
            response.headers["Content-Length"] = header
        return response

    expect_failure(download(tmp_path, receive, limits=replace(LIMITS, max_bytes=10000)), code)
    assert list(tmp_path.iterdir()) == []


def test_download_stream_limit_deletes_partial_file(tmp_path):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first-part"
            yield b"second-part"

    expect_failure(
        download(
            tmp_path,
            lambda request: httpx.Response(200, stream=Body()),
            limits=replace(LIMITS, max_bytes=12),
        ),
        "audio_too_large",
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_download_transport_errors_are_redacted_and_retryable(tmp_path, failure_type):
    def receive(request):
        raise failure_type(KEY, request=request)

    expect_failure(
        download(tmp_path, receive),
        "download_timeout" if failure_type is httpx.ReadTimeout else "download_unavailable",
        True,
    )
    assert list(tmp_path.iterdir()) == []


def test_download_total_timeout_cleans_a_partial_stream(tmp_path):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            await asyncio.sleep(5)

    expect_failure(
        download(
            tmp_path,
            lambda request: httpx.Response(200, stream=Body()),
            limits=replace(LIMITS, download_timeout_seconds=0.01),
        ),
        "download_timeout",
        True,
    )
    assert list(tmp_path.iterdir()) == []


def test_download_cancellation_cleans_a_partial_stream(tmp_path):
    async def scenario():
        started = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"partial"
                started.set()
                await asyncio.sleep(5)

        task = asyncio.create_task(
            download(tmp_path, lambda request: httpx.Response(200, stream=Body()))
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert list(tmp_path.iterdir()) == []


def test_download_never_replaces_or_removes_an_existing_file(tmp_path):
    existing = tmp_path / "source.audio"
    existing.write_bytes(b"preserved")
    expect_failure(
        download(tmp_path, lambda request: httpx.Response(200, content=b"new")),
        "audio_storage_unavailable",
        True,
    )
    assert existing.read_bytes() == b"preserved"


@pytest.mark.parametrize("identifier", ["../outside", "bad-id", "https://example.com/", ""])
def test_download_does_not_interpolate_untrusted_identifiers(tmp_path, identifier):
    def forbidden(request):
        pytest.fail("Invalid identity must not cause a network request")

    expect_failure(
        download_audio(
            identifier,
            uuid4(),
            tmp_path,
            bot_base_url="http://bot:8001",
            bot_api_key=SecretStr(KEY),
            transport=httpx.MockTransport(forbidden),
        ),
        "invalid_processing_identity",
    )


@pytest.mark.parametrize(
    "origin",
    [
        "file:///tmp/audio",
        "http://user:secret@bot",
        "http://bot/path",
        "http://bot/?token=secret",
        "http://bot:invalid",
    ],
)
def test_download_rejects_unsafe_configured_origin(tmp_path, origin):
    def forbidden(request):
        pytest.fail("Invalid origin must not cause a network request")

    expect_failure(download(tmp_path, forbidden, origin=origin), "bot_configuration")


def test_audio_workspace_is_private_and_removed_on_exception(tmp_path):
    root = tmp_path / "private"
    with pytest.raises(RuntimeError):
        with audio_workspace(root) as directory:
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            (directory / "source.audio").write_bytes(b"synthetic")
            raise RuntimeError("synthetic processing failure")
    assert not directory.exists()
    assert list(root.iterdir()) == []


def test_stale_cleanup_preserves_active_new_unrelated_and_symlinked_directories(tmp_path):
    root = tmp_path / "private"
    with audio_workspace(root) as active:
        old = time.time() - 10000
        abandoned = root / "voice-abandoned"
        abandoned.mkdir()
        (abandoned / "source.audio").write_bytes(b"synthetic")
        untouched = root / "unrelated"
        untouched.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (root / "voice-linked").symlink_to(external, target_is_directory=True)
        lock_link = root / "voice-bad-lock"
        lock_link.mkdir()
        (lock_link / ".active").symlink_to(external / "secret")
        new = root / "voice-new"
        new.mkdir()
        for directory in (active, abandoned, untouched, lock_link):
            os.utime(directory, (old, old))
        assert cleanup_stale_audio(root, 100) == 1
        assert not abandoned.exists()
        assert all(path.exists() for path in (active, untouched, external, new, lock_link))
    assert cleanup_stale_audio(tmp_path / "missing") == 0


def test_audio_workspace_rejects_symlinked_storage_root(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(VoiceFailure, match="audio_storage_unavailable"):
        with audio_workspace(link):
            pytest.fail("The symlinked storage root must not be accepted")


@pytest.fixture
def synthetic_audio(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and FFprobe are required for actual decoding checks")

    def create(extension="ogg", duration=0.25, extra=()):
        output = tmp_path / f"synthetic-{uuid4()}.{extension}"
        codec = {"ogg": "libopus", "mp3": "libmp3lame", "m4a": "aac"}[extension]
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=48000:duration={duration}",
                "-c:a",
                codec,
                *extra,
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        assert completed.returncode == 0, "Synthetic audio fixture generation failed"
        return output

    return create


@pytest.mark.parametrize("extension", ["ogg", "mp3", "m4a"])
def test_real_ffmpeg_normalizes_entire_supported_recording(tmp_path, synthetic_audio, extension):
    source = synthetic_audio(extension, duration=0.25)
    output = asyncio.run(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, max_duration_seconds=2))
    )
    assert output.name == "speech.wav"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with wave.open(str(output), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert 0.24 <= audio.getnframes() / 16000 <= 0.30
        assert len(audio.readframes(audio.getnframes())) == audio.getnframes() * 2


def test_real_ffmpeg_rejects_corrupt_and_disallowed_formats_without_output(
    tmp_path, synthetic_audio
):
    corrupt = tmp_path / "corrupt.ogg"
    corrupt.write_bytes(b"not an audio recording")
    expect_failure(prepare_audio(corrupt, tmp_path), "invalid_audio")
    assert not (tmp_path / "speech.wav").exists()
    playlist = tmp_path / "audio.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:10\nhttps://127.0.0.1:1/private\n")
    expect_failure(prepare_audio(playlist, tmp_path), "invalid_audio")
    assert not (tmp_path / "speech.wav").exists()


def test_real_ffmpeg_rejects_multiple_audio_streams(tmp_path, synthetic_audio):
    source = synthetic_audio("ogg", extra=("-map", "0:a", "-map", "0:a"))
    expect_failure(prepare_audio(source, tmp_path), "unsupported_audio")
    assert not (tmp_path / "speech.wav").exists()


@pytest.mark.parametrize("damage", ["last_byte", "half", "last_page", "changed_payload"])
def test_ogg_rejects_incomplete_or_corrupt_recordings_that_ffmpeg_can_partly_decode(
    tmp_path, synthetic_audio, damage
):
    source = synthetic_audio(duration=2)
    complete = source.read_bytes()
    if damage == "last_byte":
        damaged = complete[:-1]
    elif damage == "half":
        damaged = complete[: len(complete) // 2]
    elif damage == "last_page":
        damaged = complete[: complete.rfind(b"OggS")]
    else:
        damaged = complete[:-1] + bytes([complete[-1] ^ 1])
    source.write_bytes(damaged)
    expect_failure(prepare_audio(source, tmp_path), "invalid_audio")
    assert not (tmp_path / "speech.wav").exists()


def test_verified_telegram_download_can_omit_eos_without_losing_decoded_samples(
    tmp_path, synthetic_audio
):
    source = synthetic_audio(duration=0.25)
    complete = bytearray(source.read_bytes())
    page_start = complete.rfind(b"OggS")
    complete[page_start + 5] &= ~4
    complete[page_start + 22 : page_start + 26] = b"\x00" * 4
    # Independent bit-at-a-time fixture CRC, without borrowing the validator's table.
    checksum = 0
    for value in complete[page_start:]:
        checksum ^= value << 24
        for _ in range(8):
            checksum = ((checksum << 1) ^ (0x04C11DB7 if checksum & 0x80000000 else 0)) & 0xFFFFFFFF
    complete[page_start + 22 : page_start + 26] = checksum.to_bytes(4, "little")
    source.write_bytes(complete)
    expect_failure(prepare_audio(source, tmp_path), "invalid_audio")
    reference = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=True,
    ).stdout
    output = asyncio.run(prepare_audio(source, tmp_path, allow_missing_ogg_eos=True))
    with wave.open(str(output), "rb") as recording:
        assert recording.getnframes() >= 4000
        assert recording.readframes(recording.getnframes()) == reference
    assert source.read_bytes() == complete


@pytest.mark.parametrize("damage", ["partial_page", "changed_checksum"])
def test_telegram_missing_eos_option_still_rejects_incomplete_or_corrupt_pages(
    tmp_path, synthetic_audio, damage
):
    source = synthetic_audio(duration=2)
    complete = source.read_bytes()
    source.write_bytes(
        complete[:-1] if damage == "partial_page" else complete[:-1] + bytes([complete[-1] ^ 1])
    )
    expect_failure(prepare_audio(source, tmp_path, allow_missing_ogg_eos=True), "invalid_audio")
    assert not (tmp_path / "speech.wav").exists()


def test_real_ffmpeg_rejects_duration_and_output_size_without_truncation(tmp_path, synthetic_audio):
    source = synthetic_audio(duration=0.5)
    expect_failure(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, max_duration_seconds=0.1)),
        "audio_too_long",
    )
    expect_failure(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, max_wav_bytes=1000)),
        "audio_too_large",
    )
    assert not (tmp_path / "speech.wav").exists()


def test_ogg_at_exact_duration_limit_is_not_rejected_for_encoder_delay(tmp_path, synthetic_audio):
    source = synthetic_audio(duration=0.25)
    output = asyncio.run(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, max_duration_seconds=0.25))
    )
    with wave.open(str(output), "rb") as recording:
        assert recording.getnframes() == 4000


def test_decoded_samples_enforce_duration_even_when_probe_metadata_omits_it(
    tmp_path, synthetic_audio, monkeypatch
):
    source = synthetic_audio(duration=0.5)

    async def missing_duration(source, limits, **kwargs):
        return None

    monkeypatch.setattr("app.voice_media._probe_audio", missing_duration)
    expect_failure(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, max_duration_seconds=0.1)),
        "audio_too_long",
    )
    assert not (tmp_path / "speech.wav").exists()


def test_real_ffmpeg_timeout_kills_child_and_leaves_no_output(tmp_path, synthetic_audio):
    source = synthetic_audio()
    expect_failure(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, conversion_timeout_seconds=0.001)),
        "conversion_timeout",
    )
    assert not (tmp_path / "speech.wav").exists()


def test_conversion_does_not_destroy_an_existing_output(tmp_path, synthetic_audio):
    source = synthetic_audio()
    existing = tmp_path / "speech.wav"
    existing.write_bytes(b"preserved")
    expect_failure(prepare_audio(source, tmp_path), "audio_storage_unavailable", True)
    assert existing.read_bytes() == b"preserved"


def test_conversion_rejects_oversized_input_before_spawning_tools(tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.write_bytes(b"x" * 101)

    async def forbidden(*arguments):
        pytest.fail("Oversized inputs must not start media tools")

    monkeypatch.setattr("app.voice_media._start_tool", forbidden)
    expect_failure(
        prepare_audio(source, tmp_path, limits=replace(LIMITS, max_bytes=100)), "audio_too_large"
    )


@pytest.mark.parametrize("chunks", [[b"abc", b"d", b"efg", b"h"], [b"abc"]])
def test_pcm_pipe_boundaries_preserve_every_sample_or_reject_an_incomplete_sample(
    tmp_path, monkeypatch, chunks
):
    source = tmp_path / "input"
    source.write_bytes(b"synthetic")

    class Process:
        returncode = None

        def __init__(self):
            self.stdout = self
            self.parts = iter([*chunks, b""])

        async def read(self, count):
            return next(self.parts)

        async def wait(self):
            self.returncode = 0
            return 0

    async def probe(source, limits, **kwargs):
        return None

    async def start(*arguments):
        return Process()

    monkeypatch.setattr("app.voice_media._probe_audio", probe)
    monkeypatch.setattr("app.voice_media._start_tool", start)
    if len(b"".join(chunks)) % 2:
        expect_failure(prepare_audio(source, tmp_path), "invalid_audio")
        assert not (tmp_path / "speech.wav").exists()
    else:
        output = asyncio.run(prepare_audio(source, tmp_path))
        with wave.open(str(output), "rb") as recording:
            assert recording.getnframes() == 4
            assert recording.readframes(4) == b"abcdefgh"


def test_cancelling_conversion_kills_decoder_and_removes_partial_wav(tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.write_bytes(b"synthetic")

    async def scenario():
        started = asyncio.Event()

        class Process:
            returncode = None
            killed = False
            drained = False

            def __init__(self):
                self.stdout = self
                self.reads = 0

            async def read(self, count):
                self.reads += 1
                if self.reads == 1:
                    return b"ab"
                started.set()
                await asyncio.sleep(5)

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def communicate(self):
                self.drained = True
                return b"", b""

        process = Process()

        async def probe(source, limits, **kwargs):
            return None

        async def start(*arguments):
            return process

        monkeypatch.setattr("app.voice_media._probe_audio", probe)
        monkeypatch.setattr("app.voice_media._start_tool", start)
        task = asyncio.create_task(prepare_audio(source, tmp_path))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.drained

    asyncio.run(scenario())
    assert not (tmp_path / "speech.wav").exists()


@pytest.mark.parametrize(
    "values",
    [
        {"max_bytes": 0},
        {"max_bytes": 19_000_001},
        {"max_duration_seconds": 601},
        {"max_duration_seconds": float("nan")},
        {"max_wav_bytes": 44},
        {"download_timeout_seconds": 31},
        {"conversion_timeout_seconds": 0},
    ],
)
def test_media_limits_cannot_disable_application_caps(values):
    with pytest.raises(ValueError):
        MediaLimits(**values)
