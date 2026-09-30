import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from app import services
from app.live_outbox import dispatch_live_once
from app.models import OutboxEvent
from sqlalchemy import select


def test_only_committed_changes_are_published(app, users, settings):
    published = []
    factory = app.state.session_factory
    with factory.begin() as db:
        task, _ = services.create_task(
            db,
            UUID(users[0]["id"]),
            "Never put this content in Redis",
            "telegram_text",
            bot_identity=settings.bot_identity,
            message_id=123,
        )
        task_id = str(task.id)
        assert dispatch_live_once(factory, settings, lambda *args: published.append(args)) == 0
    assert dispatch_live_once(factory, settings, lambda *args: published.append(args)) == 1
    assert published == [
        (
            settings.live_channel,
            json.dumps(
                {
                    "type": "task_created",
                    "owner_id": users[0]["id"],
                    "task_id": task_id,
                    "revision": 1,
                },
                separators=(",", ":"),
            ),
        )
    ]
    assert dispatch_live_once(factory, settings, lambda *args: published.append(args)) == 0


def test_rolled_back_mutation_never_publishes(app, users, settings):
    factory = app.state.session_factory
    with pytest.raises(RuntimeError):
        with factory.begin() as db:
            services.create_task(
                db,
                UUID(users[0]["id"]),
                "Rolled back",
                "telegram_text",
                bot_identity=settings.bot_identity,
                message_id=124,
            )
            raise RuntimeError("rollback")
    assert dispatch_live_once(factory, settings, lambda *_: pytest.fail("published rollback")) == 0


def test_ambiguous_publish_retries_same_revision_without_losing_intent(app, make_task, settings):
    make_task()
    factory = app.state.session_factory
    published = []

    def ambiguous(channel, payload):
        published.append(payload)
        raise TimeoutError("synthetic Redis timeout after delivery")

    with pytest.raises(TimeoutError):
        dispatch_live_once(factory, settings, ambiguous)
    with factory() as db:
        assert db.scalar(select(OutboxEvent)).published_at is None
    assert (
        dispatch_live_once(factory, settings, lambda channel, payload: published.append(payload))
        == 1
    )
    assert published[0] == published[1]
    with factory() as db:
        assert db.scalar(select(OutboxEvent)).published_at is not None


def test_concurrent_dispatchers_skip_claimed_rows(app, make_task, settings):
    make_task()
    make_task()
    factory = app.state.session_factory
    entered, release = Event(), Event()
    first, second = [], []

    def hold_publish(channel, payload):
        first.append(json.loads(payload))
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(dispatch_live_once, factory, settings, hold_publish, limit=1)
        try:
            assert entered.wait(5)
            assert (
                dispatch_live_once(
                    factory, settings, lambda channel, payload: second.append(json.loads(payload))
                )
                == 1
            )
        finally:
            release.set()
        assert waiting.result(timeout=5) == 1
    assert len(first) == len(second) == 1
    assert first[0]["revision"] != second[0]["revision"]
    assert dispatch_live_once(factory, settings, lambda *_: pytest.fail("duplicate dispatch")) == 0


def test_status_and_delete_publish_even_after_task_removed(app, make_task, users, settings):
    task = make_task()
    factory = app.state.session_factory
    with factory.begin() as db:
        services.change_status(db, UUID(users[0]["id"]), UUID(task["id"]), "completed", 1)
    with factory.begin() as db:
        services.delete_task(db, UUID(users[0]["id"]), UUID(task["id"]), 2)
    payloads = []
    assert (
        dispatch_live_once(
            factory, settings, lambda channel, payload: payloads.append(json.loads(payload))
        )
        == 3
    )
    assert [(p["type"], p["revision"]) for p in payloads] == [
        ("task_created", 1),
        ("task_updated", 2),
        ("task_deleted", 3),
    ]
    assert all(set(p) == {"type", "owner_id", "task_id", "revision"} for p in payloads)
