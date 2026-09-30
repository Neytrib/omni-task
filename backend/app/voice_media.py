"""Bounded private audio download, full decoding, normalization, and temporary cleanup."""

import asyncio
import errno
import fcntl
import json
import math
import os
import shutil
import stat
import tempfile
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

from app.voice_provider import VoiceFailure, retry_after

SAMPLE_RATE = 16_000
SAMPLE_BYTES = 2
PROBE_OUTPUT_LIMIT = 65_536
FORMATS = "ogg,mp3,mov"


@dataclass(frozen=True)
class MediaLimits:
    max_bytes: int = 19_000_000
    max_duration_seconds: float = 600
    download_timeout_seconds: float = 30
    conversion_timeout_seconds: float = 30
    max_wav_bytes: int = 20_000_000

    def __post_init__(self):
        if (
            not 0 < self.max_bytes <= 19_000_000
            or not 0 < self.max_duration_seconds <= 600
            or not 44 < self.max_wav_bytes <= 20_000_000
            or not 0 < self.download_timeout_seconds <= 30
            or not 0 < self.conversion_timeout_seconds <= 30
        ):
            raise ValueError("Voice media limits must be positive and within the application caps")


DEFAULT_MEDIA_LIMITS = MediaLimits()


def _private_root(root: Path) -> Path:
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise VoiceFailure("audio_storage_unavailable", False)
    root.chmod(0o700)
    return root


@contextmanager
def audio_workspace(root: Path) -> Iterator[Path]:
    """An active lock also prevents the crash-cleanup sweep deleting another worker's files."""
    directory = Path(tempfile.mkdtemp(prefix="voice-", dir=_private_root(root)))
    try:
        with (directory / ".active").open("xb") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
    finally:
        shutil.rmtree(directory)


def cleanup_stale_audio(root: Path, older_than_seconds: float = 3600) -> int:
    """Remove only old, owned job directories with no active process lock."""
    if older_than_seconds < 0:
        raise ValueError("Audio retention age must be nonnegative")
    if not Path(root).exists():
        return 0
    directory = _private_root(root)
    cutoff = time.time() - older_than_seconds
    removed = 0
    for candidate in directory.iterdir():
        if not candidate.name.startswith("voice-"):
            continue
        try:
            info = candidate.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mtime >= cutoff
            ):
                continue
            lock_path = candidate / ".active"
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(candidate)
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            if error.errno == errno.ELOOP:
                continue  # Never follow a substituted lock symlink.
            raise
    return removed


def _safe_bot_origin(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        _ = parsed.port  # Validate invalid or out-of-range ports before constructing a request.
    except ValueError:
        raise VoiceFailure("bot_configuration", False) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VoiceFailure("bot_configuration", False)
    return origin.rstrip("/")


async def download_audio(
    request_id: UUID | str,
    lease_token: UUID | str,
    directory: Path,
    *,
    bot_base_url: str,
    bot_api_key: SecretStr,
    limits: MediaLimits = DEFAULT_MEDIA_LIMITS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Path:
    try:
        identifier, lease = UUID(str(request_id)), UUID(str(lease_token))
    except (ValueError, TypeError, AttributeError):
        raise VoiceFailure("invalid_processing_identity", False) from None
    url = f"{_safe_bot_origin(bot_base_url)}/internal/voice/{identifier}/audio"
    destination = Path(directory) / "source.audio"
    created = False
    try:
        async with asyncio.timeout(limits.download_timeout_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(limits.download_timeout_seconds, connect=3),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers={
                        "Authorization": f"Bearer {bot_api_key.get_secret_value()}",
                        "X-Correlation-ID": str(identifier),
                    },
                    json={"lease_token": str(lease)},
                ) as response:
                    status = response.status_code
                    if status == 429 or status >= 500:
                        raise VoiceFailure(
                            "download_unavailable",
                            True,
                            retry_after(response.headers.get("retry-after")),
                        )
                    if status in {401, 403}:
                        raise VoiceFailure("bot_authentication", False)
                    if status == 413:
                        raise VoiceFailure("audio_too_large", False)
                    if status == 404:
                        raise VoiceFailure("audio_unavailable", False)
                    if status != 200:
                        raise VoiceFailure("download_rejected", False)
                    length = response.headers.get("content-length")
                    if length is not None:
                        try:
                            if int(length) > limits.max_bytes or int(length) < 0:
                                raise VoiceFailure("audio_too_large", False)
                        except ValueError:
                            raise VoiceFailure("invalid_audio", False) from None
                    count = 0
                    with destination.open("xb") as file:
                        created = True
                        destination.chmod(0o600)
                        async for chunk in response.aiter_bytes():
                            count += len(chunk)
                            if count > limits.max_bytes:
                                raise VoiceFailure("audio_too_large", False)
                            file.write(chunk)
                    if count == 0:
                        raise VoiceFailure("empty_audio", False)
        return destination
    except (TimeoutError, httpx.TimeoutException):
        if created:
            destination.unlink(missing_ok=True)
        raise VoiceFailure("download_timeout", True) from None
    except httpx.HTTPError:
        if created:
            destination.unlink(missing_ok=True)
        raise VoiceFailure("download_unavailable", True) from None
    except OSError:
        if created:
            destination.unlink(missing_ok=True)
        raise VoiceFailure("audio_storage_unavailable", True) from None
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


async def _start_tool(*arguments: str) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise VoiceFailure("media_tool_unavailable", False) from None


async def _stop_tool(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        await process.communicate()


def _ogg_crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        checksum = value << 24
        for _ in range(8):
            checksum = (
                ((checksum << 1) ^ 0x04C11DB7) if checksum & 0x80000000 else checksum << 1
            ) & 0xFFFFFFFF
        table.append(checksum)
    return tuple(table)


OGG_CRC_TABLE = _ogg_crc_table()


async def _validate_complete_ogg(source: Path, *, allow_missing_eos: bool = False) -> None:
    """Require complete, ordered, checksummed pages, and normally an EOS marker.

    RFC 3533 section 6 defines the fixed header, lacing table, sequence and CRC.
    Some Telegram clients omit EOS. Only verified complete Telegram downloads may
    opt out of that marker check; every received page must still be intact.
    """
    serial = None
    sequence = 0
    ended = False
    with source.open("rb") as file:
        while header := file.read(27):
            if (
                len(header) != 27
                or header[:5] != b"OggS\x00"
                or ended
                or header[5] & ~7
                or int.from_bytes(header[18:22], "little") != sequence
            ):
                raise VoiceFailure("invalid_audio", False)
            page_serial = header[14:18]
            if serial is None:
                if not header[5] & 2:
                    raise VoiceFailure("invalid_audio", False)
                serial = page_serial
            elif page_serial != serial or header[5] & 2:
                raise VoiceFailure("unsupported_audio", False)
            lacing = file.read(header[26])
            payload = file.read(sum(lacing))
            if len(lacing) != header[26] or len(payload) != sum(lacing):
                raise VoiceFailure("invalid_audio", False)
            checksum = 0
            page = header[:22] + b"\x00" * 4 + header[26:] + lacing + payload
            for value in page:
                checksum = ((checksum << 8) & 0xFFFFFFFF) ^ OGG_CRC_TABLE[(checksum >> 24) ^ value]
            if checksum != int.from_bytes(header[22:26], "little"):
                raise VoiceFailure("invalid_audio", False)
            ended = bool(header[5] & 4)
            sequence += 1
            await asyncio.sleep(0)  # Bound cancellation latency during the full-file scan.
    if serial is None or (not ended and not allow_missing_eos):
        raise VoiceFailure("invalid_audio", False)


async def _probe_audio(
    source: Path, limits: MediaLimits, *, allow_missing_ogg_eos: bool = False
) -> None:
    process = await _start_tool(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        FORMATS,
        "-show_entries",
        "stream=codec_type,codec_name,duration:format=format_name,duration",
        "-of",
        "json",
        str(source.resolve()),
    )
    try:
        output = bytearray()
        while chunk := await process.stdout.read(8192):
            if len(output) + len(chunk) > PROBE_OUTPUT_LIMIT:
                raise VoiceFailure("invalid_audio", False)
            output.extend(chunk)
        if await process.wait() != 0:
            raise VoiceFailure("invalid_audio", False)
        try:
            metadata = json.loads(output)
            streams = metadata["streams"]
            media_format = metadata["format"]
            names = set(media_format["format_name"].split(","))
            if len(streams) != 1 or streams[0]["codec_type"] != "audio":
                raise VoiceFailure("unsupported_audio", False)
            stream = streams[0]
            codec = stream["codec_name"]
            if not (
                (names == {"ogg"} and codec == "opus")
                or (names == {"mp3"} and codec == "mp3")
                or ("mov" in names and codec in {"aac", "alac", "opus", "mp3"})
            ):
                raise VoiceFailure("unsupported_audio", False)
            if names == {"ogg"}:
                await _validate_complete_ogg(source, allow_missing_eos=allow_missing_ogg_eos)
            for value in (media_format.get("duration"), stream.get("duration")):
                if value in (None, "N/A"):
                    continue  # Full bounded decoding below still checks the actual sample count.
                duration = float(value)
                if not math.isfinite(duration) or duration <= 0:
                    raise VoiceFailure("invalid_audio", False)
                # Container durations can include encoder delay; the decoded sample
                # count below enforces the exact limit without rejecting an exact-limit Opus file.
                if duration > limits.max_duration_seconds + 0.1:
                    raise VoiceFailure("audio_too_long", False)
        except (ValueError, TypeError, KeyError, IndexError):
            raise VoiceFailure("invalid_audio", False) from None
    finally:
        await _stop_tool(process)


async def prepare_audio(
    source: Path,
    directory: Path,
    *,
    limits: MediaLimits = DEFAULT_MEDIA_LIMITS,
    allow_missing_ogg_eos: bool = False,
) -> Path:
    """Decode fully or fail; never cut output to limits.

    Allow missing Ogg EOS only after the bot has completed the bounded Telegram
    download and verified its byte count against available Telegram file sizes.
    """
    source, destination = Path(source), Path(directory) / "speech.wav"
    process = None
    created = False
    try:
        if source.is_symlink() or not source.is_file():
            raise VoiceFailure("invalid_audio", False)
        if not 0 < source.stat().st_size <= limits.max_bytes:
            raise VoiceFailure("audio_too_large", False)
        async with asyncio.timeout(limits.conversion_timeout_seconds):
            await _probe_audio(source, limits, allow_missing_ogg_eos=allow_missing_ogg_eos)
            process = await _start_tool(
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                FORMATS,
                "-err_detect",
                "explode",
                "-threads",
                "1",
                "-i",
                str(source.resolve()),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            )
            duration_bytes = int(limits.max_duration_seconds * SAMPLE_RATE) * SAMPLE_BYTES
            output_bytes = min(duration_bytes, limits.max_wav_bytes - 44)
            decoded_bytes = 0
            with destination.open("xb") as file:
                created = True
                destination.chmod(0o600)
                with wave.open(file, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(SAMPLE_BYTES)
                    wav.setframerate(SAMPLE_RATE)
                    remainder = b""
                    while chunk := await process.stdout.read(65_536):
                        decoded_bytes += len(chunk)
                        if decoded_bytes > output_bytes:
                            code = (
                                "audio_too_long"
                                if duration_bytes <= output_bytes
                                else "audio_too_large"
                            )
                            raise VoiceFailure(code, False)
                        chunk = remainder + chunk
                        aligned = len(chunk) - len(chunk) % SAMPLE_BYTES
                        wav.writeframesraw(chunk[:aligned])
                        remainder = chunk[aligned:]
            if await process.wait() != 0 or decoded_bytes % SAMPLE_BYTES:
                raise VoiceFailure("invalid_audio", False)
            if decoded_bytes == 0:
                raise VoiceFailure("empty_audio", False)
            return destination
    except TimeoutError:
        if created:
            destination.unlink(missing_ok=True)
        raise VoiceFailure("conversion_timeout", False) from None
    except OSError:
        if created:
            destination.unlink(missing_ok=True)
        raise VoiceFailure("audio_storage_unavailable", True) from None
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if process is not None:
            await _stop_tool(process)
