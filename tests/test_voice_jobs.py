"""Exercise job transaction boundaries with real PostgreSQL and no external provider."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from app.config import Settings
from app.jobs import voice_tasks
from app.jobs.dispatcher import dispatch_once
from app.models import Notification, ProcessingRequest, Task
from app.services import utcnow
from app.voice_provider import VoiceFailure
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def test_voice_lease_cannot_expire_before_notification_hard_deadline(settings):
    values = settings.model_dump()
    values["voice_lease_seconds"] = 60
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_cached_provider_success_survives_task_commit_failure(
    api, settings, db_engine, bot_headers, users, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="omni_task.voice")
    settings.transcription_provider = "fake"
    settings.voice_retry_base_seconds = 0
    settings.voice_max_attempts = 1
    response = api.post(
        "/internal/bot/processing-requests",
        headers=bot_headers,
        json={
            "telegram_user_id": users[0]["telegram_user_id"],
            "message_id": 791,
            "file_id": "synthetic-file",
            "duration_seconds": 1,
            "file_size": 700,
            "acknowledgement_message_id": 792,
        },
    )
    assert response.status_code == 201
    identifier = response.json()["id"]
    factory = sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(voice_tasks, "runtime", lambda: (settings, factory))
    complete = "  Whole transcript\nWith its last line preserved.  "
    provider = AsyncMock(return_value=complete)
    monkeypatch.setattr(voice_tasks, "transcribe_audio", provider)
    original = voice_tasks.voice.finish_transcription
    attempts = 0

    def interrupted_commit(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # The task was flushed but this transaction must roll back; transcript
            # was already committed by the job's previous short transaction.
            original(*args, **kwargs)
            raise RuntimeError("Synthetic persistence interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(voice_tasks.voice, "finish_transcription", interrupted_commit)
    voice_tasks.transcription.run(identifier)
    with factory() as db:
        request = db.get(ProcessingRequest, UUID(identifier))
        assert request.state == "queued" and request.cached_transcript == complete
        assert request.attempts == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
    voice_tasks.transcription.run(identifier)
    voice_tasks.transcription.run(identifier)
    assert provider.await_count == 1
    with factory() as db:
        request = db.get(ProcessingRequest, UUID(identifier))
        assert request.state == "succeeded" and request.cached_transcript is None
        assert request.attempts == 1
        assert db.scalar(select(Task.content)) == complete
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
        notification_id = str(db.scalar(select(Notification.id)))

    delivery = AsyncMock(
        side_effect=[
            VoiceFailure("notification_unavailable", True),
            {"outcome": "sent", "message_id": 793},
        ]
    )
    monkeypatch.setattr(voice_tasks, "send_notification", delivery)
    voice_tasks.notification.run(notification_id)
    voice_tasks.notification.run(notification_id)
    assert provider.await_count == 1 and delivery.await_count == 2
    with factory() as db:
        assert db.get(Notification, UUID(notification_id)).state == "succeeded"
        assert db.scalar(select(func.count()).select_from(Task)) == 1

    events = [
        record.omni_event
        for record in caplog.records
        if getattr(record, "omni_event", {}).get("correlation_id") == identifier
    ]
    names = [event["event"] for event in events]
    assert "transcript_saved" in names and "transcription_finished" in names
    assert "transcription_failed" in names and "notification_failed" in names
    assert "notification_finished" in names
    assert all(
        event["job_id"] == notification_id
        for event in events
        if event["event"].startswith("notification_")
    )
    assert complete not in caplog.text


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (403, False)])
def test_notification_transport_classifies_retryable_errors(
    settings, monkeypatch, status, retryable
):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, json={"ignored": True}, headers={"Retry-After": "20"})
    )
    monkeypatch.setattr(
        voice_tasks.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw)
    )
    with pytest.raises(VoiceFailure) as caught:
        asyncio.run(voice_tasks.send_notification("synthetic", "lease", settings))
    assert caught.value.retryable is retryable
    assert caught.value.retry_after_seconds == (20 if retryable else None)


def test_failed_dispatch_logs_only_correlated_metadata_and_retains_work(
    api, settings, db_engine, bot_headers, users, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="omni_task.voice")
    response = api.post(
        "/internal/bot/processing-requests",
        headers=bot_headers,
        json={
            "telegram_user_id": users[0]["telegram_user_id"],
            "message_id": 795,
            "file_id": "synthetic-private-file-reference",
            "duration_seconds": 1,
            "file_size": 700,
            "acknowledgement_message_id": 796,
        },
    )
    assert response.status_code == 201
    identifier = response.json()["id"]
    factory = sessionmaker(db_engine, expire_on_commit=False)
    exposed = "redis://private-user:synthetic-private-password@redis:6379/15"

    def unavailable(*args, **kwargs):
        raise ConnectionError(exposed)

    assert dispatch_once(factory, settings, publisher=unavailable) == 0
    with factory() as db:
        request = db.get(ProcessingRequest, UUID(identifier))
        assert request.state == "queued" and request.last_dispatched_at is not None
        assert db.scalar(select(func.count()).select_from(Task)) == 0
    later = utcnow() + timedelta(seconds=settings.voice_redispatch_seconds + 1)
    monkeypatch.setattr(voice_tasks.voice, "utcnow", lambda: later)
    published = []

    def restored(name, **payload):
        published.append((name, payload))

    assert dispatch_once(factory, settings, publisher=restored) == 1
    assert published == [
        (
            "voice.transcription",
            {
                "args": [identifier],
                "queue": "transcription",
                "retry": False,
                "ignore_result": True,
            },
        )
    ]
    monkeypatch.setattr(voice_tasks, "runtime", lambda: (settings, factory))
    monkeypatch.setattr(
        voice_tasks, "transcribe_audio", AsyncMock(return_value="Synthetic accepted transcript")
    )
    voice_tasks.transcription.run(identifier)
    with factory() as db:
        notification_id = str(db.scalar(select(Notification.id)))
    assert dispatch_once(factory, settings, publisher=restored) == 1
    assert published[1] == (
        "voice.notification",
        {
            "args": [notification_id],
            "queue": "notifications",
            "retry": False,
            "ignore_result": True,
        },
    )
    events = [
        record.omni_event
        for record in caplog.records
        if getattr(record, "omni_event", {}).get("correlation_id") == identifier
        and record.omni_event["event"].startswith("voice_dispatch")
    ]
    assert [event["event"] for event in events] == [
        "voice_dispatch_delayed",
        "voice_dispatched",
        "voice_dispatched",
    ]
    assert [event["job_id"] for event in events] == [identifier, identifier, notification_id]
    assert exposed not in caplog.text
    assert "synthetic-private-file-reference" not in caplog.text
