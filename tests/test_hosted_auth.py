"""Real-PostgreSQL auth checks for a separate HTTPS dashboard and API origin.

HTTPX does not simulate browser cookie partitioning. These verify wire headers,
server authorization and lifecycle; hosted Arc acceptance is also required.
"""

from contextlib import ExitStack
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from app.browser_transport import session_cookie
from app.config import Settings
from app.main import create_app, get_db
from app.models import BrowserSession, LoginLink
from app.services import utcnow
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

PAGES_ORIGIN = "https://pages.example"
API_ORIGIN = "https://api.example"
SOCKET_URL = "wss://api.example/api/ws/tasks"


@pytest.fixture
def hosted_settings(settings):
    return Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "app_env": "production",
            "dashboard_origin": PAGES_ORIGIN,
            "dashboard_url": f"{PAGES_ORIGIN}/project/",
            "cookie_name": "__Host-omni_session",
            "cookie_secure": True,
            "cookie_samesite": "none",
            "cookie_partitioned": True,
            "live_enabled": True,
            "live_heartbeat_seconds": 0.05,
        },
    )


@pytest.fixture
def hosted_app(hosted_settings, clean_database, monkeypatch):
    async def subscriber(_settings, hub, stop):
        hub.set_live(True)
        await stop.wait()

    async def outbox(_factory, _settings, stop):
        await stop.wait()

    monkeypatch.setattr("app.main.subscriber_loop", subscriber)
    monkeypatch.setattr("app.live_outbox.outbox_loop", outbox)
    return create_app(hosted_settings)


@pytest.fixture
def hosted_api(hosted_app):
    with TestClient(hosted_app, base_url=API_ORIGIN) as client:
        yield client


@pytest.fixture
def hosted_login(hosted_api, bot_headers):
    def issue(telegram_id=820001):
        registered = hosted_api.post(
            "/internal/bot/users",
            headers=bot_headers,
            json={"telegram_user_id": telegram_id, "private_chat_id": telegram_id},
        )
        assert registered.status_code == 200
        response = hosted_api.post(
            "/internal/bot/login-links",
            headers=bot_headers,
            json={"telegram_user_id": telegram_id},
        )
        assert response.status_code == 200
        return response.json()

    return issue


@pytest.fixture
def hosted_browser(hosted_app, hosted_login):
    with ExitStack() as stack:

        def connect(telegram_id=820001):
            client = stack.enter_context(TestClient(hosted_app, base_url=API_ORIGIN))
            client.headers["Origin"] = PAGES_ORIGIN
            response = client.post(
                "/api/auth/exchange", json={"token": hosted_login(telegram_id)["token"]}
            )
            assert response.status_code == 200
            result = client.get("/api/session")
            assert result.status_code == 200
            client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
            return client

        yield connect


def assert_cors(response):
    assert response.headers["access-control-allow-origin"] == PAGES_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in response.headers["vary"]


def assert_hosted_cookie(header):
    assert header.startswith("__Host-omni_session=")
    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert {"httponly", "secure", "partitioned", "samesite=none", "path=/"} <= attributes
    assert not any(part.startswith("domain=") for part in attributes)


def test_hosted_login_url_token_fragment_and_single_use(hosted_api, hosted_login):
    link = hosted_login()
    url = urlsplit(link["url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{PAGES_ORIGIN}/project/"
    assert url.fragment == f"token={link['token']}"
    assert not url.query
    for method in ("get", "head"):
        assert getattr(hosted_api, method)("/api/auth/exchange").status_code == 405
    response = hosted_api.post(
        "/api/auth/exchange", headers={"Origin": PAGES_ORIGIN}, json={"token": link["token"]}
    )
    assert response.status_code == 200
    assert_cors(response)
    assert_hosted_cookie(response.headers["set-cookie"])
    assert hosted_api.get("/api/session", headers={"Origin": PAGES_ORIGIN}).status_code == 200
    assert (
        hosted_api.post(
            "/api/auth/exchange", headers={"Origin": PAGES_ORIGIN}, json={"token": link["token"]}
        ).status_code
        == 401
    )


def test_expired_hosted_login_does_not_set_cookie(hosted_api, hosted_app, hosted_login):
    link = hosted_login()
    with hosted_app.state.session_factory.begin() as db:
        db.execute(update(LoginLink).values(expires_at=utcnow() - timedelta(seconds=1)))
    response = hosted_api.post(
        "/api/auth/exchange", headers={"Origin": PAGES_ORIGIN}, json={"token": link["token"]}
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert_cors(response)


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_hosted_cors_preflight_supports_real_api_operations(hosted_api, method):
    response = hosted_api.options(
        "/api/tasks",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type,x-csrf-token,idempotency-key,if-match",
        },
    )
    assert response.status_code == 200
    assert_cors(response)
    assert "*" not in response.headers["access-control-allow-methods"]
    assert "*" not in response.headers["access-control-allow-headers"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": PAGES_ORIGIN + "/"},
        {"Access-Control-Request-Headers": "authorization"},
        {"Access-Control-Request-Method": "PUT"},
    ],
)
def test_hosted_cors_rejects_unapproved_origin_headers_methods(hosted_api, overrides):
    response = hosted_api.options(
        "/api/tasks",
        headers={"Origin": PAGES_ORIGIN, "Access-Control-Request-Method": "POST", **overrides},
    )
    assert response.status_code == 400
    if "Origin" in overrides:
        assert "access-control-allow-origin" not in response.headers


def test_cors_does_not_open_internal_bot_routes(hosted_api, hosted_browser, bot_headers):
    preflight = hosted_api.options(
        "/internal/bot/users",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 401
    assert "access-control-allow-origin" not in preflight.headers
    client = hosted_browser()
    for extra, expected in (({}, 401), (bot_headers, 403)):
        response = client.get("/internal/bot/tasks?telegram_user_id=820001", headers=extra)
        assert response.status_code == expected
        assert "access-control-allow-origin" not in response.headers


def test_cors_headers_are_present_on_auth_csrf_validation_and_operational_errors(
    hosted_api, hosted_browser, hosted_app
):
    response = hosted_api.get("/api/tasks", headers={"Origin": PAGES_ORIGIN})
    assert response.status_code == 401
    assert_cors(response)
    client = hosted_browser()
    csrf = client.headers.pop("X-CSRF-Token")
    response = client.post("/api/auth/logout")
    assert response.status_code == 403
    assert_cors(response)
    client.headers["X-CSRF-Token"] = csrf
    response = client.post("/api/tasks", json={}, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 422
    assert_cors(response)

    def failed_database():
        raise RuntimeError("synthetic private database failure")

    hosted_app.dependency_overrides[get_db] = failed_database
    response = client.get("/api/tasks")
    assert response.status_code == 503
    assert "synthetic private" not in response.text
    assert_cors(response)


def test_hosted_two_user_task_isolation_and_session_bound_csrf(hosted_browser, dashboard_create):
    alice, bob = hosted_browser(820001), hosted_browser(820002)
    created = dashboard_create(alice)
    assert created.status_code == 201
    task = created.json()
    assert bob.get("/api/tasks").json()["items"] == []
    assert bob.get(f"/api/tasks/{task['id']}").status_code == 404
    assert (
        bob.patch(
            f"/api/tasks/{task['id']}", json={"status": "completed"}, headers={"If-Match": "1"}
        ).status_code
        == 404
    )
    assert bob.delete(f"/api/tasks/{task['id']}", headers={"If-Match": "1"}).status_code == 404
    bob.headers["X-CSRF-Token"] = alice.headers["X-CSRF-Token"]
    assert dashboard_create(bob).status_code == 403
    assert alice.get(f"/api/tasks/{task['id']}").status_code == 200


def test_cookie_expiration_keeps_partition_and_other_cookie_headers(hosted_settings):
    response = Response()
    response.set_cookie("other", "synthetic")
    session_cookie(response, hosted_settings, clear=True)
    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    assert headers[0].startswith("other=synthetic;")
    assert "Partitioned" not in headers[0]
    assert_hosted_cookie(headers[1])
    assert "Max-Age=0" in headers[1]
    assert "expires=" in headers[1].lower()


def test_hosted_logout_revokes_cookie_and_existing_socket(hosted_browser, hosted_api):
    client = hosted_browser()
    credential = client.cookies.get("__Host-omni_session")
    with client.websocket_connect(SOCKET_URL) as socket:
        assert socket.receive_json()["type"] == "ready"
        response = client.post("/api/auth/logout")
        assert response.status_code == 204
        assert_cors(response)
        assert_hosted_cookie(response.headers["set-cookie"])
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.get("/api/session").status_code == 401
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                socket.receive_json()
        assert closed.value.code == 4401
    hosted_api.cookies.set("__Host-omni_session", credential)
    assert hosted_api.get("/api/tasks").status_code == 401


def test_hosted_socket_session_expiry_and_two_user_isolation(
    hosted_browser, hosted_app, dashboard_create
):
    alice, bob = hosted_browser(820001), hosted_browser(820002)
    alice_id = alice.get("/api/session").json()["user"]["id"]
    with (
        alice.websocket_connect(SOCKET_URL) as first,
        bob.websocket_connect(SOCKET_URL) as second,
    ):
        assert first.receive_json()["revision"] == 0
        assert second.receive_json()["revision"] == 0
        task = dashboard_create(alice).json()
        alice.portal.call(
            hosted_app.state.live_hub.dispatch,
            {"type": "task_created", "owner_id": alice_id, "task_id": task["id"], "revision": 1},
        )
        for _ in range(20):
            event = first.receive_json()
            if event["type"] == "task_created":
                assert event["task_id"] == task["id"]
                break
        else:
            pytest.fail("Owner did not receive its event")
        assert second.receive_json() == {"type": "heartbeat", "revision": 0, "live": True}
        with hosted_app.state.session_factory.begin() as db:
            db.execute(
                update(BrowserSession)
                .where(BrowserSession.owner_id == UUID(alice_id))
                .values(expires_at=utcnow() - timedelta(seconds=1))
            )
        with pytest.raises(WebSocketDisconnect) as expired:
            while True:
                first.receive_json()
        assert expired.value.code == 4401
        assert alice.get("/api/session").status_code == 401
        assert bob.get("/api/session").status_code == 200


@pytest.mark.parametrize("origin", [API_ORIGIN, "https://attacker.example", "null"])
def test_hosted_socket_still_requires_exact_dashboard_origin(hosted_browser, origin):
    client = hosted_browser()
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(SOCKET_URL, headers={"Origin": origin}):
            pytest.fail("Unexpected cross-origin socket acceptance")
    assert rejected.value.code == 4403
