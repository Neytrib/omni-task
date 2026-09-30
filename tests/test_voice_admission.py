"""Durable voice admission uses real PostgreSQL, including cross-owner races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import pytest
from app import services, voice
from app.errors import AppError
from app.models import Notification, ProcessingRequest, SourceReceipt, Task
from fastapi.testclient import TestClient
from sqlalchemy import func, select

ENDPOINT = "/internal/bot/processing-requests"


def submit(api, bot_headers, users, message=1, owner=0, **changes):
    return api.post(
        ENDPOINT,
        headers=bot_headers,
        json={
            "telegram_user_id": users[owner]["telegram_user_id"],
            "message_id": message,
            "file_id": "synthetic_admission_file",
            "duration_seconds": 3,
            "file_size": 200,
            **changes,
        },
    )


def settle(app, result, *, state="failed", age_hours=0):
    with app.state.session_factory.begin() as db:
        request = db.get(ProcessingRequest, UUID(result.json()["id"]))
        request.state = state
        request.created_at = db.scalar(select(func.clock_timestamp())) - timedelta(hours=age_hours)


@pytest.mark.parametrize(
    "field,code,second_owner",
    [
        ("voice_user_pending_limit", "voice_user_busy", 0),
        ("voice_global_pending_limit", "voice_service_busy", 1),
        ("voice_user_daily_limit", "voice_user_quota", 0),
        ("voice_global_daily_limit", "voice_service_quota", 1),
    ],
)
def test_limits_reject_without_retaining_receipt_or_job(
    api, app, settings, bot_headers, users, field, code, second_owner
):
    setattr(settings, field, 1)
    assert submit(api, bot_headers, users).status_code == 201
    rejected = submit(api, bot_headers, users, message=2, owner=second_owner)
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == code
    with app.state.session_factory() as db:
        for model in (SourceReceipt, ProcessingRequest):
            assert db.scalar(select(func.count()).select_from(model)) == 1
        for model in (Task, Notification):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_duplicate_and_conflict_keep_their_meaning_when_all_limits_are_full(
    api, settings, bot_headers, users
):
    settings.voice_user_pending_limit = settings.voice_global_pending_limit = 1
    settings.voice_user_daily_limit = settings.voice_global_daily_limit = 1
    first = submit(api, bot_headers, users, acknowledgement_message_id=123)
    assert first.status_code == 201
    duplicate = submit(api, bot_headers, users, acknowledgement_message_id=456)
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == first.json()["id"]
    assert duplicate.json()["acknowledgement_message_id"] == 123
    conflict = submit(api, bot_headers, users, file_id="different_synthetic_file")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "source_conflict"


@pytest.mark.parametrize("state", ["queued", "processing"])
def test_old_outstanding_work_still_counts_and_rejected_source_can_be_retried(
    api, app, settings, bot_headers, users, state
):
    settings.voice_user_pending_limit = 1
    first = submit(api, bot_headers, users)
    settle(app, first, state=state, age_hours=25)
    assert submit(api, bot_headers, users, message=2).json()["error"]["code"] == "voice_user_busy"
    settle(app, first, state="failed", age_hours=25)
    assert submit(api, bot_headers, users, message=2).status_code == 201


@pytest.mark.parametrize("state", ["failed", "succeeded"])
def test_terminal_outcomes_keep_daily_allowance_until_window_expires(
    api, app, settings, bot_headers, users, state
):
    settings.voice_user_daily_limit = 1
    first = submit(api, bot_headers, users)
    settle(app, first, state=state, age_hours=23)
    assert submit(api, bot_headers, users, message=2).json()["error"]["code"] == "voice_user_quota"
    assert submit(api, bot_headers, users).status_code == 200
    settle(app, first, state=state, age_hours=25)
    assert submit(api, bot_headers, users, message=2).status_code == 201


def test_worker_completion_and_deletion_do_not_refund_daily_admission(
    api, app, settings, bot_headers, users
):
    settings.voice_user_daily_limit = settings.voice_user_pending_limit = 1
    first = submit(api, bot_headers, users)
    identifier = UUID(first.json()["id"])
    # Already accepted work can recover even after intake limits fill.
    with app.state.session_factory.begin() as db:
        claimed = voice.claim_transcription(db, identifier, settings)
        assert claimed is not None
        lease = claimed["lease_token"]
        assert voice.save_transcript(db, identifier, lease, "Synthetic quota transcript", settings)
        result = voice.finish_transcription(db, identifier, lease, settings)
        task = db.get(Task, UUID(result["task_id"]))
        services.delete_task(db, task.owner_id, task.id, task.version)
    assert submit(api, bot_headers, users, message=2).json()["error"]["code"] == "voice_user_quota"
    duplicate = submit(api, bot_headers, users)
    assert duplicate.status_code == 200 and duplicate.json()["deleted"]


@pytest.mark.parametrize(
    "field,owners,code",
    [
        ("voice_user_pending_limit", [0, 0], "voice_user_busy"),
        ("voice_global_pending_limit", [0, 1], "voice_service_busy"),
        ("voice_user_daily_limit", [0, 0], "voice_user_quota"),
        ("voice_global_daily_limit", [0, 1], "voice_service_quota"),
    ],
)
def test_concurrent_admissions_cannot_oversubscribe(
    app, settings, bot_headers, users, field, owners, code
):
    setattr(settings, field, 1)
    barrier = Barrier(2)

    def competing_request(index):
        # Independent clients/DB sessions; the global limit must work across owners.
        client = TestClient(app)
        try:
            barrier.wait(timeout=5)
            return submit(client, bot_headers, users, message=index + 1, owner=owners[index])
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing_request, range(2)))
    assert sorted(response.status_code for response in results) == [201, 429]
    assert (
        next(response for response in results if response.status_code == 429).json()["error"][
            "code"
        ]
        == code
    )
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ProcessingRequest)) == 1
        assert db.scalar(select(func.count()).select_from(SourceReceipt)) == 1


def test_rollback_does_not_consume_admission(api, app, settings, bot_headers, users):
    settings.voice_user_pending_limit = settings.voice_user_daily_limit = 1
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with app.state.session_factory.begin() as db:
            services.create_processing_request(
                db,
                UUID(users[0]["id"]),
                "synthetic_rollback_file",
                settings.bot_identity,
                10,
                settings=settings,
                duration_seconds=3,
            )
            raise RuntimeError("synthetic rollback")
    assert submit(api, bot_headers, users).status_code == 201


def test_shared_service_enforces_limits_without_http(api, app, settings, bot_headers, users):
    settings.voice_global_daily_limit = 1
    assert submit(api, bot_headers, users).status_code == 201
    with pytest.raises(AppError) as caught:
        with app.state.session_factory.begin() as db:
            services.create_processing_request(
                db,
                UUID(users[1]["id"]),
                "synthetic_service_file",
                settings.bot_identity,
                12,
                settings=settings,
                duration_seconds=3,
            )
    assert caught.value.code == "voice_service_quota"
