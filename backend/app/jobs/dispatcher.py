"""Recoverable PostgreSQL dispatch intents; Redis deliveries contain UUIDs only."""

import asyncio
import logging

from app import voice
from omni_logging import log_event

logger = logging.getLogger("omni_task.voice")


def dispatch_once(factory, settings, publisher=None) -> int:
    if publisher is None:
        from app.jobs.celery_app import celery_app

        publisher = celery_app.send_task
    with factory.begin() as db:
        entries = voice.claim_dispatches(db, settings)
    sent = 0
    for entry in entries:
        queue = "transcription" if entry["kind"] == "transcription" else "notifications"
        try:
            publisher(
                "voice." + entry["kind"],
                args=[str(entry["id"])],
                queue=queue,
                retry=False,
                ignore_result=True,
            )
            sent += 1
            log_event(
                logger,
                "voice_dispatched",
                correlation_id=entry["request_id"],
                job_id=entry["id"],
                queue=queue,
                outcome="published",
            )
        except Exception:
            # The committed dispatch timestamp expires; a later tick retries the intent.
            log_event(
                logger,
                "voice_dispatch_delayed",
                level=logging.WARNING,
                correlation_id=entry["request_id"],
                job_id=entry["id"],
                queue=queue,
                error_code="broker_unavailable",
            )
    return sent


async def dispatcher_loop(factory, settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(dispatch_once, factory, settings)
        except Exception:
            log_event(
                logger,
                "voice_dispatcher_delayed",
                level=logging.WARNING,
                error_code="persistence_unavailable",
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
