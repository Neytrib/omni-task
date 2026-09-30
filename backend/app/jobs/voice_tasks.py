"""Separate Celery jobs for transcription and delivery, backed by fenced DB leases."""

import asyncio
import logging
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import voice
from app.config import Settings
from app.jobs.celery_app import celery_app
from app.voice_media import (
    MediaLimits,
    audio_workspace,
    cleanup_stale_audio,
    download_audio,
    prepare_audio,
)
from app.voice_provider import VoiceFailure, build_provider, retry_after, validate_transcript
from omni_logging import log_event

logger = logging.getLogger("omni_task.voice")


@lru_cache(maxsize=1)
def runtime():
    settings = Settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    return settings, sessionmaker(engine, expire_on_commit=False)


def renew(factory, identifier, lease, settings) -> bool:
    try:
        with factory.begin() as db:
            return voice.renew_transcription(db, identifier, lease, settings)
    except Exception:
        return False


async def transcribe_audio(identifier, lease, snapshot, settings, factory) -> str:
    limits = MediaLimits(
        max_bytes=settings.voice_max_bytes,
        max_duration_seconds=settings.voice_max_duration_seconds,
        download_timeout_seconds=settings.voice_download_timeout_seconds,
        conversion_timeout_seconds=settings.voice_conversion_timeout_seconds,
    )
    # Honor the provider selected at intake. A later configuration switch must not
    # silently turn an accepted demo recording into a billable provider request.
    selected = settings.model_copy(
        update={
            "transcription_provider": snapshot.get("provider_name", settings.transcription_provider)
        }
    )
    provider = build_provider(selected)
    root = Path(settings.voice_temp_dir)
    cleanup_stale_audio(root)

    async def work():
        with audio_workspace(root) as directory:
            original = await download_audio(
                identifier,
                lease,
                directory,
                bot_base_url=settings.bot_base_url,
                bot_api_key=settings.bot_api_key,
                limits=limits,
            )
            # Some Telegram clients omit the stream-ending page flag.
            # The bot has already checked the complete download against Telegram's size.
            audio = await prepare_audio(
                original, directory, limits=limits, allow_missing_ogg_eos=True
            )
            return validate_transcript(await provider.transcribe(audio))

    job = asyncio.create_task(work())

    async def heartbeat():
        while not job.done():
            await asyncio.sleep(max(1, settings.voice_lease_seconds / 3))
            if not await asyncio.to_thread(renew, factory, identifier, lease, settings):
                job.cancel()
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        return await asyncio.wait_for(job, timeout=180)
    except TimeoutError:
        raise VoiceFailure("processing_timeout", True) from None
    except asyncio.CancelledError:
        raise VoiceFailure("lease_lost", True) from None
    finally:
        job.cancel()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


@celery_app.task(
    name="voice.transcription", ignore_result=True, soft_time_limit=195, time_limit=210
)
def transcription(request_id: str) -> None:
    settings, factory = runtime()
    identifier = None
    try:
        identifier = UUID(request_id)
        with factory.begin() as db:
            snapshot = voice.claim_transcription(db, identifier, settings)
        if snapshot is None:
            log_event(logger, "transcription_skipped", correlation_id=identifier, job_id=identifier)
            return
    except Exception:
        log_event(
            logger,
            "transcription_unclaimed",
            level=logging.WARNING,
            correlation_id=identifier,
            job_id=identifier,
            error_code="persistence_or_identifier",
        )
        return
    lease = snapshot["lease_token"]
    log_event(
        logger,
        "transcription_claimed",
        correlation_id=identifier,
        job_id=identifier,
        attempt=snapshot["attempts"],
    )
    try:
        if snapshot.get("cached_transcript") is None:
            transcript = asyncio.run(
                transcribe_audio(identifier, lease, snapshot, settings, factory)
            )
            with factory.begin() as db:
                if not voice.save_transcript(db, identifier, lease, transcript, settings):
                    log_event(
                        logger,
                        "transcription_result_discarded",
                        correlation_id=identifier,
                        job_id=identifier,
                    )
                    return
            log_event(logger, "transcript_saved", correlation_id=identifier, job_id=identifier)
        with factory.begin() as db:
            result = voice.finish_transcription(db, identifier, lease, settings)
        log_event(
            logger,
            "transcription_finished",
            correlation_id=identifier,
            job_id=identifier,
            task_id=result["task_id"] if result else None,
            outcome="succeeded" if result else "stale",
        )
    except VoiceFailure as error:
        try:
            with factory.begin() as db:
                handled = voice.fail_transcription(
                    db,
                    identifier,
                    lease,
                    error.code,
                    error.retryable,
                    settings,
                    retry_after=error.retry_after_seconds,
                )
            log_event(
                logger,
                "transcription_failed",
                level=logging.WARNING,
                correlation_id=identifier,
                job_id=identifier,
                error_code=error.code,
                outcome="recorded" if handled else "stale",
                attempt=snapshot["attempts"],
            )
        except Exception:
            log_event(
                logger,
                "transcription_recovery_pending",
                level=logging.WARNING,
                correlation_id=identifier,
                job_id=identifier,
                error_code="persistence_unavailable",
            )
    except Exception:
        # No Celery exception object/traceback can contain a transcript, key or URL.
        try:
            with factory.begin() as db:
                handled = voice.fail_transcription(
                    db, identifier, lease, "processing_interrupted", True, settings
                )
            log_event(
                logger,
                "transcription_failed",
                level=logging.WARNING,
                correlation_id=identifier,
                job_id=identifier,
                error_code="processing_interrupted",
                outcome="recorded" if handled else "stale",
                attempt=snapshot["attempts"],
            )
        except Exception:
            log_event(
                logger,
                "transcription_recovery_pending",
                level=logging.WARNING,
                correlation_id=identifier,
                job_id=identifier,
                error_code="persistence_unavailable",
            )


async def send_notification(request_id, lease, settings):
    try:
        async with asyncio.timeout(35):
            async with httpx.AsyncClient(
                base_url=settings.bot_base_url,
                headers={
                    "Authorization": "Bearer " + settings.bot_api_key.get_secret_value(),
                    "X-Correlation-ID": str(request_id),
                },
                timeout=httpx.Timeout(30, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"/internal/voice/{request_id}/notify", json={"lease_token": str(lease)}
                )
        if response.status_code == 429 or response.status_code >= 500:
            raise VoiceFailure(
                "notification_unavailable", True, retry_after(response.headers.get("Retry-After"))
            )
        if response.status_code >= 400:
            raise VoiceFailure("notification_rejected", False)
        result = response.json()
        if result.get("outcome") not in {"sent", "suppressed"}:
            raise VoiceFailure("invalid_notification_response", True)
        if result.get("message_id") is not None and (
            type(result["message_id"]) is not int or result["message_id"] <= 0
        ):
            raise VoiceFailure("invalid_notification_response", True)
        return result
    except (httpx.HTTPError, TimeoutError):
        raise VoiceFailure("notification_unavailable", True) from None
    except (ValueError, AttributeError):
        raise VoiceFailure("invalid_notification_response", True) from None


@celery_app.task(name="voice.notification", ignore_result=True, soft_time_limit=45, time_limit=60)
def notification(notification_id: str) -> None:
    settings, factory = runtime()
    identifier = None
    try:
        identifier = UUID(notification_id)
        with factory.begin() as db:
            snapshot = voice.claim_notification(db, identifier, settings)
        if snapshot is None:
            log_event(logger, "notification_skipped", job_id=identifier)
            return
    except Exception:
        log_event(
            logger,
            "notification_unclaimed",
            level=logging.WARNING,
            job_id=identifier,
            error_code="persistence_or_identifier",
        )
        return
    lease = snapshot["lease_token"]
    correlation_id = snapshot["request_id"]
    log_event(
        logger,
        "notification_claimed",
        correlation_id=correlation_id,
        job_id=identifier,
        attempt=snapshot["attempts"],
    )
    try:
        result = asyncio.run(send_notification(snapshot["request_id"], lease, settings))
        with factory.begin() as db:
            finished = voice.finish_notification(
                db, identifier, lease, settings, sent_message_id=result.get("message_id")
            )
        log_event(
            logger,
            "notification_finished",
            correlation_id=correlation_id,
            job_id=identifier,
            outcome=result["outcome"] if finished else "stale",
        )
    except VoiceFailure as error:
        try:
            with factory.begin() as db:
                handled = voice.fail_notification(
                    db,
                    identifier,
                    lease,
                    error.code,
                    error.retryable,
                    settings,
                    retry_after=error.retry_after_seconds,
                )
            log_event(
                logger,
                "notification_failed",
                level=logging.WARNING,
                correlation_id=correlation_id,
                job_id=identifier,
                error_code=error.code,
                outcome="recorded" if handled else "stale",
                attempt=snapshot["attempts"],
            )
        except Exception:
            log_event(
                logger,
                "notification_recovery_pending",
                level=logging.WARNING,
                correlation_id=correlation_id,
                job_id=identifier,
                error_code="persistence_unavailable",
            )
    except Exception:
        try:
            with factory.begin() as db:
                handled = voice.fail_notification(
                    db, identifier, lease, "notification_interrupted", True, settings
                )
            log_event(
                logger,
                "notification_failed",
                level=logging.WARNING,
                correlation_id=correlation_id,
                job_id=identifier,
                error_code="notification_interrupted",
                outcome="recorded" if handled else "stale",
                attempt=snapshot["attempts"],
            )
        except Exception:
            log_event(
                logger,
                "notification_recovery_pending",
                level=logging.WARNING,
                correlation_id=correlation_id,
                job_id=identifier,
                error_code="persistence_unavailable",
            )
