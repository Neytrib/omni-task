"""Private production-routing fixture client; used only by scripts/check_production.py."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import traceback
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

ORIGIN = "https://" + os.environ["DOMAIN"]
TLS = ssl.create_default_context(cafile="/trust/root.crt")
BOT_HEADERS = {"Authorization": "Bearer " + os.environ["BOT_API_KEY"]}


def until(socket, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(socket.recv(timeout=max(0.05, deadline - time.monotonic())))
        assert set(message) in ({"type", "revision", "live"}, {"type", "task_id", "revision"})
        if predicate(message):
            return message
    raise AssertionError("Expected secure WebSocket notification did not arrive")


def browser():
    return httpx.Client(base_url=ORIGIN, verify=TLS, trust_env=False, timeout=5)


def login(bot, client, identity):
    link = bot.post(
        "/internal/bot/login-links", headers=BOT_HEADERS, json={"telegram_user_id": identity}
    )
    link.raise_for_status()
    url = link.json()["url"]
    assert url.startswith(ORIGIN + "/login#token=")
    token = parse_qs(urlsplit(url).fragment)["token"][0]
    assert client.get(url).status_code == 200  # GET must not consume the fragment token.
    response = client.post("/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": token})
    response.raise_for_status()
    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    cookie = parsed["omni_session"]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/" and not cookie["domain"] and int(cookie["max-age"]) > 0
    assert (
        client.post(
            "/api/auth/exchange", headers={"Origin": ORIGIN}, json={"token": token}
        ).status_code
        == 401
    )
    session = client.get("/api/session")
    session.raise_for_status()
    return token, cookie.value, session.json()["csrf_token"]


def websocket(credential=None, *, origin=ORIGIN):
    headers = {"Cookie": "omni_session=" + credential} if credential else {}
    return connect(
        "wss://" + os.environ["DOMAIN"] + "/api/ws/tasks",
        ssl=TLS,
        origin=origin,
        additional_headers=headers,
        proxy=None,
        open_timeout=5,
        close_timeout=2,
    )


def main():
    if os.environ.get("FAILURE_PROBE") == "true":
        sentinel = os.environ["LOG_SENTINEL"]
        with browser() as client:
            response = client.get(
                "/login/" + sentinel,
                params={"token": sentinel},
                headers={
                    "Cookie": "omni_session=" + sentinel,
                    "Authorization": "Bearer " + sentinel,
                },
            )
            assert response.status_code == 502
        print(json.dumps({"upstream_failure": "observed"}))
        return

    secrets = []
    with (
        httpx.Client(base_url="http://api:8000", trust_env=False, timeout=5) as bot,
        browser() as alice,
        browser() as bob,
    ):
        response = alice.get("/")
        assert response.status_code == 200 and '<div id="root">' in response.text
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        import re

        asset = re.search(r'src="(/assets/[^\"]+\.js)"', response.text)
        assert asset and alice.get(asset[1]).status_code == 200
        redirected = alice.get("http://" + os.environ["DOMAIN"] + "/login")
        assert redirected.status_code == 308 and redirected.headers["location"] == ORIGIN + "/login"
        assert alice.get("/internal/bot/users", headers=BOT_HEADERS).status_code == 404
        assert alice.get("/api/health/live").status_code == 200
        assert alice.get("/api/tasks").status_code == 401
        tasks = []
        for identity in (930001, 930002):
            response = bot.post(
                "/internal/bot/users",
                headers=BOT_HEADERS,
                json={"telegram_user_id": identity, "private_chat_id": identity},
            )
            response.raise_for_status()
            response = bot.post(
                "/internal/bot/tasks",
                headers=BOT_HEADERS,
                json={
                    "telegram_user_id": identity,
                    "message_id": 1,
                    "content": f"Synthetic HTTPS task {identity}",
                },
            )
            assert response.status_code == 201
            tasks.append(response.json())
        a_token, a_cookie, a_csrf = login(bot, alice, 930001)
        b_token, b_cookie, b_csrf = login(bot, bob, 930002)
        secrets.extend([a_token, a_cookie, a_csrf, b_token, b_cookie, b_csrf])
        assert [item["id"] for item in alice.get("/api/tasks").json()["items"]] == [tasks[0]["id"]]
        assert [item["id"] for item in bob.get("/api/tasks").json()["items"]] == [tasks[1]["id"]]
        assert bob.get("/api/tasks/" + tasks[0]["id"]).status_code == 404
        assert (
            alice.post(
                "/api/tasks",
                headers={"Origin": ORIGIN, "Idempotency-Key": str(uuid4())},
                json={"content": "CSRF must reject this"},
            ).status_code
            == 403
        )
        insecure = alice.get("http://" + os.environ["DOMAIN"] + "/api/session")
        assert "cookie" not in insecure.request.headers and insecure.status_code == 308

        for credential, origin in ((None, ORIGIN), (a_cookie, "https://attacker.invalid")):
            try:
                with websocket(credential, origin=origin):
                    raise AssertionError("Unauthenticated or cross-origin WebSocket was accepted")
            except InvalidStatus as error:
                assert error.response.status_code == 403
        with websocket(a_cookie) as a_socket, websocket(b_cookie) as b_socket:
            until(
                a_socket,
                lambda item: item["type"] in {"ready", "resync", "heartbeat"} and item["live"],
            )
            until(
                b_socket,
                lambda item: item["type"] in {"ready", "resync", "heartbeat"} and item["live"],
            )
            changed = bot.patch(
                "/internal/bot/tasks/" + tasks[0]["id"],
                headers={**BOT_HEADERS, "If-Match": "1"},
                params={"telegram_user_id": 930001},
                json={"status": "completed"},
            )
            changed.raise_for_status()
            message = until(a_socket, lambda item: item["type"] == "task_updated")
            assert message["task_id"] == tasks[0]["id"] and message["revision"] == 2
            assert alice.get("/api/tasks/" + tasks[0]["id"]).json()["status"] == "completed"
            created = alice.post(
                "/api/tasks",
                headers={"Origin": ORIGIN, "X-CSRF-Token": a_csrf, "Idempotency-Key": str(uuid4())},
                json={"content": "  Complete HTTPS-created content.\nSecond line.  "},
            )
            assert created.status_code == 201
            message = until(a_socket, lambda item: item["type"] == "task_created")
            assert message["task_id"] == created.json()["id"] and message["revision"] == 3
            removed = alice.delete(
                "/api/tasks/" + created.json()["id"],
                headers={"Origin": ORIGIN, "X-CSRF-Token": a_csrf, "If-Match": "1"},
            )
            assert removed.status_code == 204
            assert until(a_socket, lambda item: item["type"] == "task_deleted")["revision"] == 4
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                message = json.loads(b_socket.recv(timeout=3))
                assert message["revision"] == 1
                if message["type"] not in {"heartbeat", "ready", "resync"}:
                    # A previously committed own hint may publish after ready.
                    assert message["type"] == "task_created"
                    assert message["task_id"] == tasks[1]["id"]
            logout = alice.post(
                "/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": a_csrf}
            )
            assert logout.status_code == 204 and alice.get("/api/tasks").status_code == 401
            try:
                until(a_socket, lambda _: False, timeout=4)
            except ConnectionClosed as error:
                assert error.rcvd.code == 4401
            else:
                raise AssertionError("Logout did not close the existing secure socket")
            assert bob.get("/api/session").status_code == 200
    print(
        json.dumps(
            {
                "https": "verified_private_ca",
                "http_redirect": "passed",
                "built_frontend": "served",
                "secure_cookie": "passed",
                "csrf": "passed",
                "owner_isolation": "passed",
                "websocket_create_update_delete": "passed",
                "websocket_origin_auth": "passed",
                "logout_socket_revocation": "passed",
                "private_secrets": secrets,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never echo HTTP responses/URLs/cookies while diagnosing fixture failures.
        frames = traceback.extract_tb(error.__traceback__)
        line = next(frame.lineno for frame in reversed(frames) if frame.filename == __file__)
        print(f"HTTPS_FIXTURE_FAILED {type(error).__name__} line={line}", file=sys.stderr)
        raise SystemExit(1) from None
