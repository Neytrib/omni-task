"""Publish committed revision hints independently of Celery job queues."""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import OutboxEvent
from omni_logging import log_event

logger = logging.getLogger("omni_task.outbox")
EVENT_TYPES = {
    "task.created": "task_created",
    "task.updated": "task_updated",
    "task.deleted": "task_deleted",
}


def dispatch_live_once(
    factory: sessionmaker,
    settings: Settings,
    publish: Callable[[str, str], object],
    *,
    limit: int = 100,
) -> int:
    """One short transaction per hint; crash/ambiguous publish may replay the same revision.

    Row locks coordinate API processes. These reads cannot see uncommitted mutations.
    Marking a hint published does not assert subscriber delivery; PostgreSQL heartbeats
    repair Pub/Sub loss even when Redis reports zero subscribers.
    """
    count = 0
    deadline = time.monotonic() + 0.5
    while count < limit:
        with factory.begin() as db:
            event = db.scalar(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                break
            payload = {
                "type": EVENT_TYPES[event.event_type],
                "owner_id": str(event.owner_id),
                "task_id": str(event.task_id),
                "revision": event.revision,
            }
            try:
                publish(settings.live_channel, json.dumps(payload, separators=(",", ":")))
            except Exception:
                log_event(
                    logger,
                    "live_publish_delayed",
                    correlation_id=event.id,
                    task_id=event.task_id,
                    revision=event.revision,
                    error_code="broker_unavailable",
                    level=logging.WARNING,
                )
                raise
            event.published_at = datetime.now(UTC)
            event_id, task_id, revision = event.id, event.task_id, event.revision
        log_event(
            logger,
            "live_hint_published",
            correlation_id=event_id,
            task_id=task_id,
            revision=revision,
        )
        count += 1
        if time.monotonic() >= deadline:
            break
    return count


async def outbox_loop(factory: sessionmaker, settings: Settings, stop: asyncio.Event) -> None:
    # Disable client-level retries: the durable row owns retries; each call is bounded.
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )
    warned = False
    try:
        while not stop.is_set():
            try:
                await asyncio.to_thread(dispatch_live_once, factory, settings, client.publish)
                warned = False
            except Exception:
                if not warned:
                    log_event(
                        logger,
                        "live_outbox_unavailable",
                        level=logging.WARNING,
                        error_code="dispatch_unavailable",
                    )
                    warned = True
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.live_dispatch_interval_seconds)
            except TimeoutError:
                pass
    finally:
        client.close()
