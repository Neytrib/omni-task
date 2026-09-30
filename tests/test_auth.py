from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from conftest import BOT_KEY, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-secret", "Basic arbitrary"])
def test_bot_authentication_precedes_identity_resolution(api, db_engine, db_tables, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    result = api.post(
        "/internal/bot/users",
        headers=headers,
        json={"telegram_user_id": 999999, "private_chat_id": 999999},
    )
    assert result.status_code == 401
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db_tables["users"])) == 0


@pytest.mark.parametrize(
    "body", [b"{invalid json", b"x" * 1_048_577], ids=["malformed", "oversized"]
)
def test_bot_authentication_precedes_body_parsing(api, body):
    response = api.post(
        "/internal/bot/users", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("browser_headers", [{"Origin": ORIGIN}, {"Cookie": "other=synthetic"}])
def test_bot_key_cannot_be_used_with_browser_context(api, bot_headers, browser_headers):
    response = api.post(
        "/internal/bot/users",
        headers={**bot_headers, **browser_headers},
        json={"telegram_user_id": 123, "private_chat_id": 123},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"telegram_user_id": 123, "private_chat_id": -123},
        {"telegram_user_id": 123, "private_chat_id": 456},
        {"telegram_user_id": 0, "private_chat_id": 0},
        {"telegram_user_id": "123", "private_chat_id": 123},
    ],
)
def test_only_valid_private_telegram_identity_is_accepted(api, bot_headers, payload):
    assert api.post("/internal/bot/users", headers=bot_headers, json=payload).status_code == 422


def test_telegram_registration_is_idempotent(api, bot_headers, users):
    result = api.post(
        "/internal/bot/users",
        headers=bot_headers,
        json={
            "telegram_user_id": users[0]["telegram_user_id"],
            "private_chat_id": users[0]["telegram_user_id"],
        },
    )
    assert result.status_code in (200, 201)
    assert result.json()["id"] == users[0]["id"]


def test_browser_session_cannot_use_bot_endpoints(browser, users):
    client = browser()
    payload = {"telegram_user_id": users[0]["telegram_user_id"]}
    assert client.post("/internal/bot/login-links", json=payload).status_code == 401
    assert client.get("/internal/bot/tasks", params=payload).status_code == 401
    assert (
        client.post(
            "/internal/bot/users", json={**payload, "private_chat_id": payload["telegram_user_id"]}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/api/session", {}),
        ("get", "/api/tasks", {}),
        ("get", f"/api/tasks/{uuid4()}", {}),
        ("post", "/api/tasks", {"json": {"content": "Unauthenticated"}}),
        ("patch", f"/api/tasks/{uuid4()}", {"json": {"status": "completed"}}),
        ("delete", f"/api/tasks/{uuid4()}", {}),
        ("post", "/api/auth/logout", {}),
    ],
)
def test_every_browser_task_operation_requires_session(api, method, path, kwargs):
    response = getattr(api, method)(
        path, headers={"Origin": ORIGIN, "Idempotency-Key": str(uuid4()), "If-Match": "1"}, **kwargs
    )
    assert response.status_code == 401


def test_login_url_uses_fragment_and_get_head_do_not_consume_token(api, login_link):
    link = login_link()
    url = urlsplit(link["url"])
    assert not url.query
    assert "token=" in url.fragment
    assert link["token"] not in url.path
    for method in ("get", "head"):
        response = getattr(api, method)("/api/auth/exchange", params={"token": link["token"]})
        assert response.status_code == 405
    result = api.post(
        "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": link["token"]}
    )
    assert result.status_code in (200, 201, 204)
    assert api.get("/api/session").status_code == 200


def test_reusing_consumed_link_fails(api, login_link):
    token = login_link()["token"]
    for expected in ((200, 201, 204), (401,)):
        response = api.post("/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": token})
        assert response.status_code in expected


def test_expired_link_and_link_revoked_by_new_issuance_fail(api, login_link, db_engine, db_tables):
    expired = login_link()["token"]
    with db_engine.begin() as connection:
        connection.execute(
            update(db_tables["login_links"]).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert (
        api.post(
            "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": expired}
        ).status_code
        == 401
    )
    old = login_link()["token"]
    new = login_link()["token"]
    assert (
        api.post("/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": old}).status_code
        == 401
    )
    assert api.post(
        "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": new}
    ).status_code in (200, 201, 204)


def test_concurrent_login_exchange_yields_one_session(app, login_link, db_engine, db_tables):
    token = login_link()["token"]
    barrier = Barrier(6)

    def exchange(_):
        with TestClient(app, base_url=ORIGIN) as client:
            barrier.wait(timeout=15)
            return client.post(
                "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": token}
            ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(exchange, range(6)))
    assert sum(status in (200, 201, 204) for status in statuses) == 1
    assert statuses.count(401) == 5
    with db_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(db_tables["sessions"])) == 1


def test_credentials_are_hashed_in_postgresql_and_cookie_is_private(
    api, login_link, db_engine, db_tables, caplog
):
    caplog.set_level(logging.INFO)
    link = login_link()
    response = api.post(
        "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": link["token"]}
    )
    assert response.status_code in (200, 201, 204)
    session_token = api.cookies.get("omni_session")
    assert session_token
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "domain=" not in cookie
    assert "max-age=" in cookie or "expires=" in cookie
    with db_engine.connect() as connection:
        links = connection.execute(select(db_tables["login_links"])).mappings().all()
        sessions = connection.execute(select(db_tables["sessions"])).mappings().all()
    assert link["token"] not in repr(links)
    assert session_token not in repr(sessions)
    assert links[0]["token_hash"] and sessions[0]["token_hash"]
    assert len(links[0]["token_hash"]) >= 32
    assert len(sessions[0]["token_hash"]) >= 32
    assert link["token"] not in caplog.text
    assert session_token not in caplog.text
    assert BOT_KEY not in caplog.text
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("referrer-policy") == "no-referrer"


def test_production_session_cookie_is_secure(settings, login_link):
    from app.config import Settings
    from app.main import create_app

    production = Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "app_env": "production",
            "cookie_secure": True,
            "dashboard_origin": "https://dashboard.example",
        },
    )
    token = login_link()["token"]
    with TestClient(create_app(production), base_url="https://dashboard.example") as client:
        response = client.post(
            "/api/auth/exchange",
            headers={"Origin": "https://dashboard.example"},
            json={"token": token},
        )
        assert response.status_code in (200, 201, 204)
        assert "; secure" in response.headers["set-cookie"].lower()
        assert client.get("/api/session").status_code == 200


def test_logout_revokes_server_session_and_replaying_cookie_fails(
    browser, app, db_engine, db_tables
):
    client = browser()
    credential = client.cookies.get("omni_session")
    response = client.post("/api/auth/logout")
    assert response.status_code in (200, 204)
    assert client.get("/api/session").status_code == 401
    with TestClient(app, base_url=ORIGIN) as replay:
        replay.cookies.set("omni_session", credential)
        assert replay.get("/api/tasks").status_code == 401
    with db_engine.connect() as connection:
        row = connection.execute(select(db_tables["sessions"])).mappings().one()
        assert row["revoked_at"] is not None


@pytest.mark.parametrize("field", ["expires_at", "revoked_at"])
def test_server_session_expiry_and_revocation_are_enforced(browser, db_engine, db_tables, field):
    client = browser()
    with db_engine.begin() as connection:
        connection.execute(
            update(db_tables["sessions"]).values({field: datetime.now(UTC) - timedelta(seconds=1)})
        )
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/tasks").status_code == 401


@pytest.mark.parametrize(
    "origin", [None, "https://attacker.example", "http://testserver.attacker.example", "null"]
)
def test_exchange_requires_exact_origin(api, login_link, origin):
    link = login_link()
    headers = {} if origin is None else {"Origin": origin}
    response = api.post("/api/auth/exchange", headers=headers, json={"token": link["token"]})
    assert response.status_code == 403
    # A rejected request must not burn the one-use token.
    assert api.post(
        "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": link["token"]}
    ).status_code in (200, 201, 204)


@pytest.mark.parametrize("operation", ["create", "status", "delete", "logout"])
@pytest.mark.parametrize("fault", ["missing_csrf", "wrong_csrf", "missing_origin", "wrong_origin"])
def test_every_browser_mutation_enforces_csrf_and_origin(
    browser, dashboard_create, operation, fault
):
    client = browser()
    created = dashboard_create(client)
    assert created.status_code in (200, 201)
    task = created.json()
    if fault == "missing_csrf":
        client.headers.pop("X-CSRF-Token")
    elif fault == "wrong_csrf":
        client.headers["X-CSRF-Token"] = "wrong"
    elif fault == "missing_origin":
        client.headers.pop("Origin")
    else:
        client.headers["Origin"] = "https://attacker.example"
    if operation == "create":
        response = dashboard_create(client)
    elif operation == "status":
        response = client.patch(
            f"/api/tasks/{task['id']}",
            json={"status": "completed"},
            headers={"If-Match": str(task["version"])},
        )
    elif operation == "delete":
        response = client.delete(
            f"/api/tasks/{task['id']}", headers={"If-Match": str(task["version"])}
        )
    else:
        response = client.post("/api/auth/logout")
    assert response.status_code == 403


def test_non_ascii_csrf_is_rejected_without_server_failure(browser):
    client = browser()
    response = client.post("/api/auth/logout", headers={b"X-CSRF-Token": b"\xff"})
    assert response.status_code == 403


def test_csrf_token_is_bound_to_session(browser, dashboard_create):
    alice, bob = browser(0), browser(1)
    bob.headers["X-CSRF-Token"] = alice.headers["X-CSRF-Token"]
    assert dashboard_create(bob).status_code == 403


def test_validation_and_operational_logs_do_not_echo_sensitive_input(
    api, bot_headers, users, caplog
):
    caplog.set_level(logging.INFO)
    marker = "SYNTHETIC_PRIVATE_BODY_MUST_NOT_BE_LOGGED"
    body = marker + "x" * 50001
    response = api.post(
        "/internal/bot/tasks",
        headers=bot_headers,
        json={"telegram_user_id": users[0]["telegram_user_id"], "message_id": 1, "content": body},
    )
    assert response.status_code == 422
    assert marker not in response.text
    assert marker not in caplog.text
    assert BOT_KEY not in caplog.text
    forged = "SYNTHETIC_INVALID_LOGIN_TOKEN_" + "x" * 300
    response = api.post("/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": forged})
    assert response.status_code in (401, 422)
    assert forged not in response.text and forged not in caplog.text


def test_default_local_profile_link_and_exchange_share_exact_origin(settings, clean_database):
    from app.config import Settings
    from app.main import create_app
    from conftest import BOT_KEY

    origin = Settings.model_fields["dashboard_origin"].default
    configuration = settings.model_copy(update={"dashboard_origin": origin})
    with TestClient(create_app(configuration), base_url=origin) as client:
        headers = {"Authorization": f"Bearer {BOT_KEY}"}
        response = client.post(
            "/internal/bot/users",
            headers=headers,
            json={"telegram_user_id": 810001, "private_chat_id": 810001},
        )
        assert response.status_code == 200
        response = client.post(
            "/internal/bot/login-links", headers=headers, json={"telegram_user_id": 810001}
        )
        assert response.status_code == 200
        link = response.json()
        # Telegram rejects dotless HTTP hosts in ordinary URL buttons; default local
        # login still stays on loopback, with matching cookie and CSRF origins.
        assert urlsplit(link["url"]).hostname == "127.0.0.1"
        rejected = client.post(
            "/api/auth/exchange",
            headers={"Origin": "http://localhost:8080"},
            json={"token": link["token"]},
        )
        assert rejected.status_code == 403
        exchange = client.post(
            "/api/auth/exchange", headers={"Origin": origin}, json={"token": link["token"]}
        )
        assert exchange.status_code == 200
        assert client.get("/api/session").status_code == 200
