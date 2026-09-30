"""Real PostgreSQL proofs for the durable voice state machine and HTTP boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from app import voice
from app.errors import AppError
from app.models import Notification, OutboxEvent, ProcessingRequest, SourceReceipt, Task, User
from app.services import delete_task, utcnow
from sqlalchemy import func, select, text


@pytest.fixture
def voice_request(api, bot_headers, users):
    body = {
        "telegram_user_id": users[0]["telegram_user_id"],
        "message_id": 911,
        "file_id": "synthetic_voice_file",
        "duration_seconds": 3,
        "file_size": 200,
        "acknowledgement_message_id": 1888,
    }
    response = api.post("/internal/bot/processing-requests", headers=bot_headers, json=body)
    assert response.status_code == 201
    return UUID(response.json()["id"]), body, response.json()


def claim(app, settings, identifier, **kwargs):
    with app.state.session_factory.begin() as db:
        return voice.claim_transcription(db, identifier, settings, **kwargs)


def complete(app, settings, identifier, transcript="  Full transcript\nՀայերեն 🧪  ", **kwargs):
    snapshot = claim(app, settings, identifier, **kwargs)
    assert snapshot is not None
    lease = snapshot["lease_token"]
    with app.state.session_factory.begin() as db:
        assert voice.save_transcript(db, identifier, lease, transcript, settings, **kwargs)
    with app.state.session_factory.begin() as db:
        result = voice.finish_transcription(db, identifier, lease, settings, **kwargs)
        assert result is not None
    return result


def test_intake_is_durable_and_duplicate_acknowledgements_are_canonical(
    app, api, bot_headers, voice_request
):
    identifier, body, original = voice_request
    replay = api.post(
        "/internal/bot/processing-requests",
        headers=bot_headers,
        json={**body, "acknowledgement_message_id": 9999},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == str(identifier)
    assert replay.json()["acknowledgement_message_id"] == 1888
    assert replay.json()["state"] == "queued" and replay.json()["task_id"] is None
    assert original["provider_name"] == "disabled"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ProcessingRequest)) == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert db.scalar(select(func.count()).select_from(Notification)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"duration_seconds": 0},
        {"duration_seconds": 601},
        {"duration_seconds": "3"},
        {"file_size": 19_000_001},
        {"file_size": -1},
        {"acknowledgement_message_id": -1},
        {"file_id": "https://forged.example/audio.ogg"},
    ],
)
def test_invalid_voice_metadata_is_rejected(api, bot_headers, voice_request, change):
    _, body, _ = voice_request
    response = api.post(
        "/internal/bot/processing-requests",
        headers=bot_headers,
        json={**body, "message_id": 912, **change},
    )
    assert response.status_code == 422


def test_metadata_duration_is_required(api, bot_headers, voice_request):
    _, body, _ = voice_request
    body.pop("duration_seconds")
    assert (
        api.post("/internal/bot/processing-requests", headers=bot_headers, json=body).status_code
        == 422
    )


def test_concurrent_claim_has_one_current_execution_lease(app, settings, voice_request):
    identifier, _, _ = voice_request
    barrier = Barrier(5)

    def acquire(_):
        barrier.wait(timeout=5)
        return claim(app, settings, identifier)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(acquire, range(5)))
    assert sum(result is not None for result in results) == 1
    with app.state.session_factory() as db:
        assert db.get(ProcessingRequest, identifier).attempts == 1


def after_owner_lock_wait(app, owner_id, clock, action):
    """Hold an actual PostgreSQL owner lock while the job's clock crosses its deadline."""
    factory = app.state.session_factory
    entered = Event()
    identity = {}

    def waiting_job():
        with factory.begin() as db:
            identity["pid"] = db.scalar(select(func.pg_backend_pid()))
            entered.set()
            return action(db)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as blocker:
            blocker.scalar(select(User).where(User.id == owner_id).with_for_update())
            future = pool.submit(waiting_job)
            assert entered.wait(timeout=5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with factory() as observer:
                    waiting = observer.scalar(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        identity,
                    )
                if waiting:
                    break
                sleep(0.01)
            else:
                raise AssertionError("Job did not block on its PostgreSQL owner lock")
            clock[0] += timedelta(seconds=1000)
        return future.result(timeout=5)


def test_expired_transcription_cannot_commit_after_owner_lock_wait(
    app, settings, voice_request, monkeypatch
):
    identifier, _, _ = voice_request
    lease = claim(app, settings, identifier)["lease_token"]
    with app.state.session_factory.begin() as db:
        assert voice.save_transcript(
            db, identifier, lease, "Retain this accepted transcript", settings
        )
        owner_id = db.get(ProcessingRequest, identifier).owner_id
    clock = [utcnow()]
    monkeypatch.setattr(voice, "utcnow", lambda: clock[0])
    result = after_owner_lock_wait(
        app,
        owner_id,
        clock,
        lambda db: voice.finish_transcription(db, identifier, lease, settings),
    )
    assert result is None
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert (
            db.get(ProcessingRequest, identifier).cached_transcript
            == "Retain this accepted transcript"
        )


def test_notification_context_rechecks_expiry_after_owner_lock_wait(
    app, settings, voice_request, monkeypatch
):
    identifier, _, _ = voice_request
    completed = complete(app, settings, identifier)
    with app.state.session_factory.begin() as db:
        delivery = voice.claim_notification(db, UUID(completed["notification_id"]), settings)
        owner_id = db.get(ProcessingRequest, identifier).owner_id
    clock = [utcnow()]
    monkeypatch.setattr(voice, "utcnow", lambda: clock[0])
    with pytest.raises(AppError) as caught:
        after_owner_lock_wait(
            app,
            owner_id,
            clock,
            lambda db: voice.notification_context(
                db, identifier, delivery["lease_token"], settings
            ),
        )
    assert caught.value.code == "lease_expired"


def test_claim_deadline_starts_after_owner_lock_wait(app, settings, voice_request, monkeypatch):
    identifier, _, _ = voice_request
    with app.state.session_factory() as db:
        owner_id = db.get(ProcessingRequest, identifier).owner_id
    clock = [utcnow()]
    monkeypatch.setattr(voice, "utcnow", lambda: clock[0])
    claimed = after_owner_lock_wait(
        app,
        owner_id,
        clock,
        lambda db: voice.claim_transcription(db, identifier, settings),
    )
    assert claimed is not None
    with app.state.session_factory() as db:
        assert db.get(ProcessingRequest, identifier).lease_expires_at == clock[0] + timedelta(
            seconds=settings.voice_lease_seconds
        )


def test_completed_transcript_creates_one_task_and_distinct_notification(
    app, settings, voice_request
):
    identifier, _, _ = voice_request
    text = "  Complete original transcript\n" + "🧪" * 10000 + "  "
    result = complete(app, settings, identifier, text)
    assert claim(app, settings, identifier) is None
    with app.state.session_factory() as db:
        request = db.get(ProcessingRequest, identifier)
        task = db.get(Task, UUID(result["task_id"]))
        notification = db.get(Notification, UUID(result["notification_id"]))
        assert task.content == text and task.status == "pending" and task.source == "telegram_voice"
        assert request.state == "succeeded" and request.cached_transcript is None
        assert notification.state == "pending" and notification.attempts == 0
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert db.get(User, request.owner_id).task_revision == 1


def test_cached_transcript_recovers_after_worker_crash_without_new_provider_attempt(
    app, settings, voice_request
):
    identifier, _, _ = voice_request
    limited = settings.model_copy(update={"voice_max_attempts": 1})
    first = claim(app, limited, identifier)
    with app.state.session_factory.begin() as db:
        assert voice.save_transcript(
            db, identifier, first["lease_token"], "Cached exactly", limited
        )
    later = utcnow() + timedelta(seconds=settings.voice_lease_seconds + 1)
    second = claim(app, limited, identifier, now=later)
    assert second["cached_transcript"] == "Cached exactly" and second["attempts"] == 1
    assert second["lease_token"] != first["lease_token"]
    with app.state.session_factory.begin() as db:
        assert not voice.save_transcript(
            db, identifier, first["lease_token"], "Stale result", limited, now=later
        )
        assert (
            voice.finish_transcription(db, identifier, first["lease_token"], limited, now=later)
            is None
        )
        assert voice.finish_transcription(db, identifier, second["lease_token"], limited, now=later)
    with app.state.session_factory() as db:
        assert db.scalar(select(Task.content)) == "Cached exactly"


@pytest.mark.parametrize(
    "transcript,error",
    [
        (" \n\t", "transcript_empty"),
        ("x" * 50001, "transcript_too_large"),
        ("bad\x00value", "transcript_invalid"),
    ],
    ids=["empty", "oversized", "invalid-unicode"],
)
def test_invalid_transcript_fails_separately_without_fake_pending_task(
    app, settings, voice_request, transcript, error
):
    identifier, _, _ = voice_request
    lease = claim(app, settings, identifier)["lease_token"]
    with app.state.session_factory.begin() as db:
        assert not voice.save_transcript(db, identifier, lease, transcript, settings)
    with app.state.session_factory() as db:
        request = db.get(ProcessingRequest, identifier)
        assert request.state == "failed" and request.error_code == error
        assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert db.scalar(select(Notification.state)) == "pending"


def test_transcription_retry_is_delayed_bounded_and_fenced(app, settings, voice_request):
    identifier, _, _ = voice_request
    now = utcnow()
    for attempt in range(1, settings.voice_max_attempts + 1):
        snapshot = claim(app, settings, identifier, now=now)
        assert snapshot["attempts"] == attempt
        with app.state.session_factory.begin() as db:
            assert voice.fail_transcription(
                db,
                identifier,
                snapshot["lease_token"],
                "provider_unavailable",
                True,
                settings,
                now=now,
            )
        with app.state.session_factory() as db:
            request = db.get(ProcessingRequest, identifier)
            if attempt < settings.voice_max_attempts:
                assert request.state == "queued" and request.next_attempt_at > now
                assert claim(app, settings, identifier, now=now) is None
                now = request.next_attempt_at + timedelta(seconds=1)
            else:
                assert request.state == "failed"
    assert claim(app, settings, identifier, now=now + timedelta(days=1)) is None
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_expired_final_attempt_becomes_terminal_failure_on_recovery(app, settings, voice_request):
    identifier, _, _ = voice_request
    limited = settings.model_copy(update={"voice_max_attempts": 1})
    claim(app, limited, identifier)
    later = utcnow() + timedelta(seconds=settings.voice_lease_seconds + 1)
    assert claim(app, limited, identifier, now=later) is None
    with app.state.session_factory() as db:
        assert db.get(ProcessingRequest, identifier).state == "failed"
        assert db.scalar(select(Notification.state)) == "pending"


def test_dispatch_intent_recovers_lost_broker_message_without_contents(
    app, settings, voice_request
):
    identifier, _, _ = voice_request
    now = utcnow()
    with app.state.session_factory.begin() as db:
        entries = voice.claim_dispatches(db, settings, now=now)
    assert entries == [
        {"kind": "transcription", "id": str(identifier), "request_id": str(identifier)}
    ]
    with app.state.session_factory.begin() as db:
        assert voice.claim_dispatches(db, settings, now=now) == []
    later = now + timedelta(seconds=settings.voice_redispatch_seconds + 1)
    with app.state.session_factory.begin() as db:
        assert voice.claim_dispatches(db, settings, now=later) == entries


def test_notification_retries_do_not_retranscribe_and_preserve_fallback_checkpoint(
    app, settings, voice_request
):
    identifier, _, _ = voice_request
    result = complete(app, settings, identifier)
    notification_id = UUID(result["notification_id"])
    with app.state.session_factory.begin() as db:
        first = voice.claim_notification(db, notification_id, settings)
        assert voice.checkpoint_notification(db, identifier, first["lease_token"], 3333, settings)
        assert voice.fail_notification(
            db, notification_id, first["lease_token"], "telegram_timeout", True, settings
        )
    with app.state.session_factory() as db:
        due = db.get(Notification, notification_id).next_attempt_at + timedelta(seconds=1)
    with app.state.session_factory.begin() as db:
        second = voice.claim_notification(db, notification_id, settings, now=due)
        context = voice.notification_context(
            db, identifier, second["lease_token"], settings, now=due
        )
        assert context["sent_message_id"] == 3333
        assert context["acknowledgement_message_id"] == 1888
        assert voice.finish_notification(
            db, notification_id, second["lease_token"], settings, now=due
        )
    with app.state.session_factory() as db:
        assert db.get(ProcessingRequest, identifier).attempts == 1
        assert db.get(Notification, notification_id).state == "succeeded"
        assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_notification_exhaustion_is_durable_without_repeating_transcription(
    app, settings, voice_request
):
    identifier, _, _ = voice_request
    notification_id = UUID(complete(app, settings, identifier)["notification_id"])
    now = utcnow()
    for _attempt in range(settings.notification_max_attempts):
        with app.state.session_factory.begin() as db:
            snapshot = voice.claim_notification(db, notification_id, settings, now=now)
            assert snapshot is not None
            voice.fail_notification(
                db,
                notification_id,
                snapshot["lease_token"],
                "telegram_timeout",
                True,
                settings,
                now=now,
            )
        with app.state.session_factory() as db:
            notification = db.get(Notification, notification_id)
            now = notification.next_attempt_at + timedelta(seconds=1)
    with app.state.session_factory() as db:
        assert db.get(Notification, notification_id).state == "failed"
        assert db.get(ProcessingRequest, identifier).attempts == 1


def test_deletion_suppresses_inflight_notification_and_never_resurrects_voice_task(
    app, settings, api, bot_headers, voice_request
):
    identifier, body, original = voice_request
    result = complete(app, settings, identifier)
    notification_id = UUID(result["notification_id"])
    with app.state.session_factory.begin() as db:
        delivery = voice.claim_notification(db, notification_id, settings)
    with app.state.session_factory.begin() as db:
        task = db.get(Task, UUID(result["task_id"]))
        delete_task(db, task.owner_id, task.id, task.version)
    with app.state.session_factory.begin() as db:
        assert voice.notification_context(db, identifier, delivery["lease_token"], settings)[
            "suppressed"
        ]
        assert not voice.finish_notification(db, notification_id, delivery["lease_token"], settings)
        assert not voice.checkpoint_notification(
            db, identifier, delivery["lease_token"], 4444, settings
        )
    replay = api.post(
        "/internal/bot/processing-requests",
        headers=bot_headers,
        json={**body, "acknowledgement_message_id": 9999},
    )
    assert replay.status_code == 200 and replay.json()["deleted"] is True
    assert replay.json()["acknowledgement_message_id"] == original["acknowledgement_message_id"]
    assert claim(app, settings, identifier) is None
    with app.state.session_factory() as db:
        assert db.get(ProcessingRequest, identifier).cached_transcript is None
        assert db.get(Notification, notification_id).state == "suppressed"
        assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert db.scalar(select(SourceReceipt.outcome)) == "deleted"


def test_adapter_contexts_require_service_auth_and_current_execution_lease(
    app, settings, api, bot_headers, voice_request
):
    identifier, _, original = voice_request
    lease = claim(app, settings, identifier)["lease_token"]
    path = f"/internal/voice/{identifier}/download-context"
    assert api.post(path, json={"lease_token": lease}).status_code == 401
    assert (
        api.post(path, headers=bot_headers, json={"lease_token": str(uuid4())}).status_code == 409
    )
    response = api.post(path, headers=bot_headers, json={"lease_token": lease})
    assert response.status_code == 200
    assert response.json()["telegram_user_id"] == 810001
    assert response.json()["acknowledgement_message_id"] == original["acknowledgement_message_id"]
    assert api.get(path, headers=bot_headers, params={"lease_token": lease}).status_code == 405


def test_notification_task_reads_latest_status(app, settings, voice_request):
    from app.services import change_status

    identifier, _, _ = voice_request
    result = complete(app, settings, identifier)
    with app.state.session_factory.begin() as db:
        task = db.get(Task, UUID(result["task_id"]))
        change_status(db, task.owner_id, task.id, "completed", task.version)
    with app.state.session_factory.begin() as db:
        delivery = voice.claim_notification(db, UUID(result["notification_id"]), settings)
        context = voice.notification_context(db, identifier, delivery["lease_token"], settings)
        assert context["task"]["status"] == "completed"
        assert "content" not in context["task"]
