"""PostgreSQL session security and bounded, content-free WebSocket fan-out."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.models import BrowserSession
from app.realtime import QUEUE_SIZE, LiveHub, decode_hint
from app.services import utcnow
from sqlalchemy import update
from starlette.websockets import WebSocketDisconnect

ORIGIN = "http://testserver"


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"live_enabled": True, "live_heartbeat_seconds": 0.1})


@pytest.fixture(autouse=True)
def isolated_live_loops(monkeypatch):
    async def subscriber(_settings, hub, stop):
        hub.set_live(True)
        await stop.wait()

    async def outbox(_factory, _settings, stop):
        await stop.wait()

    monkeypatch.setattr("app.main.subscriber_loop", subscriber)
    monkeypatch.setattr("app.live_outbox.outbox_loop", outbox)


@pytest.fixture
def sessions(api, login_link, settings):
    credentials = []
    for user in range(2):
        link = login_link(user)
        response = api.post(
            "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": link["token"]}
        )
        assert response.status_code == 200
        session = api.get("/api/session").json()
        credentials.append(
            {
                "Origin": ORIGIN,
                "Cookie": f"{settings.cookie_name}={api.cookies.get(settings.cookie_name)}",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        api.cookies.clear()
    return credentials


def dispatch(api, app, hint):
    api.portal.call(app.state.live_hub.dispatch, hint)


def next_message(socket, kind):
    for _ in range(30):
        message = socket.receive_json()
        if message["type"] == kind:
            return message
    pytest.fail(f"Did not receive {kind}")


def test_hint_validation_requires_exact_content_free_shape():
    hint = {
        "type": "task_created",
        "owner_id": str(uuid4()),
        "task_id": str(uuid4()),
        "revision": 1,
    }
    assert decode_hint(json.dumps(hint)) == hint
    assert decode_hint(json.dumps(hint).encode()) == hint
    invalid = [
        "not json",
        "[]",
        b"\xff",
        json.dumps({**hint, "content": "private"}),
        json.dumps({**hint, "type": "unexpected"}),
        json.dumps({**hint, "type": []}),
        json.dumps({**hint, "owner_id": "other"}),
        json.dumps({**hint, "task_id": 42}),
        json.dumps({**hint, "revision": True}),
        json.dumps({**hint, "revision": 0}),
        json.dumps({**hint, "revision": 2**53}),
        " " * 1025,
        None,
    ]
    for raw in invalid:
        assert decode_hint(raw) is None


def test_hub_routes_only_to_owner_and_omits_owner_field():
    async def check():
        hub = LiveHub()
        first = hub.register("first")
        same_owner = hub.register("first")
        second = hub.register("second")
        hint = {"type": "task_deleted", "owner_id": "first", "task_id": "task", "revision": 7}
        hub.dispatch(hint)
        expected = {"type": "task_deleted", "task_id": "task", "revision": 7}
        assert await first.queue.get() == expected
        assert await same_owner.queue.get() == expected
        assert second.queue.empty()
        hub.unregister(first)
        hub.dispatch(hint)
        assert first.queue.empty()
        assert same_owner.queue.qsize() == 1

    asyncio.run(check())


def test_slow_socket_queue_overflow_requests_authoritative_resync():
    async def check():
        hub = LiveHub()
        hub.set_live(True)
        connection = hub.register("first")
        for revision in range(1, QUEUE_SIZE + 2):
            hub.dispatch(
                {"type": "task_created", "owner_id": "first", "task_id": "id", "revision": revision}
            )
        assert connection.queue.qsize() == 1
        assert await connection.queue.get() == {"type": "resync", "live": True}

    asyncio.run(check())


def test_subscriber_disconnect_and_recovery_require_resync_even_without_new_revision():
    async def check():
        hub = LiveHub()
        hub.set_live(True)
        connection = hub.register("first")
        hub.set_live(False)
        hub.set_live(False)
        hub.set_live(True, force=True)
        assert await connection.queue.get() == {"type": "resync", "live": False}
        assert await connection.queue.get() == {"type": "resync", "live": True}
        assert connection.queue.empty()

    asyncio.run(check())


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.invalid", ORIGIN + "/"])
def test_socket_rejects_missing_or_forged_origin(api, sessions, origin):
    headers = {"Cookie": sessions[0]["Cookie"]}
    if origin is not None:
        headers["Origin"] = origin
    with pytest.raises(WebSocketDisconnect) as denied:
        with api.websocket_connect("/api/ws/tasks", headers=headers):
            pytest.fail("Unexpected WebSocket acceptance")
    assert denied.value.code == 4403


@pytest.mark.parametrize("query", ["owner_id=forged", "token=synthetic", "user_id=810002"])
def test_socket_rejects_query_credentials_and_forged_actor(api, sessions, query):
    with pytest.raises(WebSocketDisconnect) as denied:
        with api.websocket_connect(f"/api/ws/tasks?{query}", headers=sessions[0]):
            pytest.fail("Unexpected WebSocket acceptance")
    assert denied.value.code == 4403


def test_socket_requires_browser_session_not_bot_credential(api, bot_headers):
    with pytest.raises(WebSocketDisconnect) as denied:
        with api.websocket_connect("/api/ws/tasks", headers={"Origin": ORIGIN, **bot_headers}):
            pytest.fail("Unexpected WebSocket acceptance")
    assert denied.value.code == 4401


@pytest.mark.parametrize("credential", ["missing", "malformed", "unknown"])
def test_socket_rejects_invalid_browser_credentials(api, settings, credential):
    headers = {"Origin": ORIGIN}
    if credential != "missing":
        token = "not-a-session" if credential == "malformed" else "A" * 43
        headers["Cookie"] = f"{settings.cookie_name}={token}"
    with pytest.raises(WebSocketDisconnect) as denied:
        with api.websocket_connect("/api/ws/tasks", headers=headers):
            pytest.fail("Unexpected WebSocket acceptance")
    assert denied.value.code == 4401


def test_live_disabled_does_not_accept_socket(api, settings, sessions):
    settings.live_enabled = False
    with pytest.raises(WebSocketDisconnect) as denied:
        with api.websocket_connect("/api/ws/tasks", headers=sessions[0]):
            pytest.fail("Unexpected WebSocket acceptance")
    assert denied.value.code == 1013


def test_socket_initial_revision_and_owner_scoping(api, app, users, sessions, make_task):
    task = make_task()
    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as first:
        assert first.receive_json() == {"type": "ready", "revision": 1, "live": True}
        with api.websocket_connect("/api/ws/tasks", headers=sessions[1]) as second:
            assert second.receive_json() == {"type": "ready", "revision": 0, "live": True}
            dispatch(
                api,
                app,
                {
                    "type": "task_created",
                    "owner_id": users[0]["id"],
                    "task_id": task["id"],
                    "revision": 1,
                },
            )
            assert next_message(first, "task_created") == {
                "type": "task_created",
                "task_id": task["id"],
                "revision": 1,
            }
            # User two receives their own heartbeat, never user one's hint.
            assert second.receive_json() == {"type": "heartbeat", "revision": 0, "live": True}
    assert not app.state.live_hub.connections


def test_heartbeat_recovers_committed_change_with_no_redis_hint(api, sessions, make_task):
    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as socket:
        assert socket.receive_json()["revision"] == 0
        make_task()
        assert next_message(socket, "heartbeat") == {
            "type": "heartbeat",
            "revision": 1,
            "live": True,
        }


def test_registration_buffers_commit_racing_initial_revision_read(
    api, app, sessions, users, monkeypatch
):
    import app.realtime as realtime

    original = realtime._session_state
    calls = 0
    task_id = str(uuid4())

    async def read_state(factory, token):
        nonlocal calls
        result = await original(factory, token)
        calls += 1
        if calls == 2:
            # Simulate a commit notification arriving after the ready revision was read.
            app.state.live_hub.dispatch(
                {
                    "type": "task_created",
                    "owner_id": users[0]["id"],
                    "task_id": task_id,
                    "revision": 1,
                }
            )
        return result

    monkeypatch.setattr(realtime, "_session_state", read_state)
    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as socket:
        assert socket.receive_json() == {"type": "ready", "revision": 0, "live": True}
        assert next_message(socket, "task_created")["task_id"] == task_id


def test_existing_socket_closes_after_logout(api, sessions):
    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as socket:
        socket.receive_json()
        assert api.post("/api/auth/logout", headers=sessions[0]).status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            next_message(socket, "never")
        assert closed.value.code == 4401


def test_existing_socket_closes_after_expiry(api, app, sessions, users):
    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as socket:
        socket.receive_json()
        with app.state.session_factory.begin() as db:
            db.execute(
                update(BrowserSession)
                .where(BrowserSession.owner_id == UUID(users[0]["id"]))
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )
        with pytest.raises(WebSocketDisconnect) as closed:
            next_message(socket, "never")
        assert closed.value.code == 4401


def test_socket_read_only_protocol_rejects_forged_client_events(api, sessions):
    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as socket:
        socket.receive_json()
        socket.send_json({"type": "task_deleted", "owner_id": "forged", "task_id": str(uuid4())})
        with pytest.raises(WebSocketDisconnect) as closed:
            next_message(socket, "never")
        assert closed.value.code == 1008


def test_database_failure_closes_socket_without_leaking_details(api, sessions, monkeypatch):
    import app.realtime as realtime

    with api.websocket_connect("/api/ws/tasks", headers=sessions[0]) as socket:
        socket.receive_json()

        async def unavailable(_factory, _token):
            raise RuntimeError("private synthetic database details")

        monkeypatch.setattr(realtime, "_session_state", unavailable)
        with pytest.raises(WebSocketDisconnect) as closed:
            next_message(socket, "never")
        assert closed.value.code == 1013
        assert closed.value.reason == ""
