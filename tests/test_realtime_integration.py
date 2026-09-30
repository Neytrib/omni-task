"""Real PostgreSQL/Redis fan-out between independent API and Celery processes.

WebSocket clients here exercise the wire protocol, not a replacement browser. All
users, audio, provider responses, credentials, and Telegram adapters are synthetic.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from app import services, voice
from app.config import Settings
from app.models import BrowserSession, OutboxEvent, ProcessingRequest, Task, User
from celery import Celery
from conftest import BOT_KEY, ORIGIN
from redis import Redis
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

CONTROL_TYPES = {"ready", "heartbeat", "resync"}
TASK_TYPES = {"task_created", "task_updated", "task_deleted"}


def eventually(predicate, *, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("The isolated real-time integration condition did not converge")


def read_message(websocket, *, timeout=5):
    message = json.loads(websocket.recv(timeout=timeout))
    assert type(message["revision"]) is int and message["revision"] >= 0
    if message["type"] in CONTROL_TYPES:
        assert set(message) == {"type", "revision", "live"}
        assert type(message["live"]) is bool
    else:
        assert message["type"] in TASK_TYPES
        assert set(message) == {"type", "revision", "task_id"}
        UUID(message["task_id"])
    return message


def receive_until(websocket, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = read_message(websocket, timeout=max(0.01, deadline - time.monotonic()))
        if predicate(message):
            return message
    raise AssertionError("Expected WebSocket notification did not arrive")


def expect_hint(websocket, event_type, task, revision):
    message = receive_until(websocket, lambda item: item["type"] in TASK_TYPES)
    assert message == {"type": event_type, "task_id": task["id"], "revision": revision}


def expect_isolated_heartbeat(websocket, revision):
    # Reject leaked hints, not merely a missing task in an HTTP snapshot.
    # Drain queued controls and wait a whole (1s) heartbeat interval, so an old
    # heartbeat cannot hide a leaked mutation queued immediately behind it.
    deadline = time.monotonic() + 1.2
    heartbeat_received = False
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            message = read_message(websocket, timeout=remaining)
        except TimeoutError:
            break
        assert message["type"] in CONTROL_TYPES
        assert message["revision"] == revision
        if message["type"] == "heartbeat":
            heartbeat_received = True
    assert heartbeat_received


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class LiveStack:
    def __init__(self, database_url, db_engine, tmp_path):
        self.database_url = database_url
        self.factory = sessionmaker(db_engine, expire_on_commit=False)
        self.directory = tmp_path
        self.redis_url = os.environ["TEST_REDIS_URL"]
        self.channel = f"live-test-{uuid4().hex}"
        self.redis = Redis.from_url(self.redis_url, decode_responses=True)
        self.resources = ExitStack()
        self.processes = [None, None]
        self.listeners = []
        self.urls = []
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "DATABASE_URL": database_url,
                "REDIS_URL": self.redis_url,
                "BOT_API_KEY": BOT_KEY,
                "BOT_IDENTITY": "live-integration-bot",
                "DASHBOARD_ORIGIN": ORIGIN,
                "APP_ENV": "test",
                "COOKIE_SECURE": "false",
                "LIVE_ENABLED": "true",
                "LIVE_CHANNEL": self.channel,
                "LIVE_HEARTBEAT_SECONDS": "1",
                "VOICE_DISPATCH_ENABLED": "false",
                "TRANSCRIPTION_PROVIDER": "fake",
                "OPENAI_API_KEY": "",
                "ALLOW_PAID_TRANSCRIPTION": "false",
            }
        )
        # Keep bound listeners in the parent so a stopped API can restart on the
        # same address without racing another process for an ephemeral port.
        for _ in range(2):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.listeners.append(listener)
            self.urls.append(f"http://127.0.0.1:{listener.getsockname()[1]}")

    def start(self, index):
        output = self.resources.enter_context((self.directory / f"api-{index}.log").open("a"))
        listener = self.listeners[index]
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:create_app",
                "--factory",
                "--fd",
                str(listener.fileno()),
                "--ws",
                "websockets-sansio",
                "--ws-max-size",
                "1024",
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            env=self.environment,
            pass_fds=(listener.fileno(),),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        self.processes[index] = process

        def ready():
            assert process.poll() is None, "Isolated API exited before becoming ready"
            try:
                response = httpx.get(self.urls[index] + "/api/health/ready", timeout=0.3)
            except httpx.HTTPError:
                return False
            return response.status_code == 200

        eventually(ready)

    def browser(self, user):
        response = self.bot.post(
            "/internal/bot/login-links", json={"telegram_user_id": user["telegram_user_id"]}
        )
        assert response.status_code in {200, 201}
        token = parse_qs(urlsplit(response.json()["url"]).fragment)["token"][0]
        browser = self.resources.enter_context(
            httpx.Client(base_url=self.urls[0], headers={"Origin": ORIGIN}, timeout=5)
        )
        assert browser.post("/api/auth/exchange", json={"token": token}).status_code == 200
        response = browser.get("/api/session")
        assert response.status_code == 200
        browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return browser

    def websocket(self, browser, index=0):
        websocket = self.resources.enter_context(
            connect(
                self.urls[index].replace("http://", "ws://") + "/api/ws/tasks",
                origin=ORIGIN,
                additional_headers={"Cookie": "omni_session=" + browser.cookies["omni_session"]},
                proxy=None,
                close_timeout=2,
            )
        )
        receive_until(websocket, lambda item: item["type"] in CONTROL_TYPES and item["live"])
        return websocket

    def bot_task(self, *, message_id, content="Synthetic Telegram text task"):
        response = self.bot.post(
            "/internal/bot/tasks",
            json={
                "telegram_user_id": self.users[0]["telegram_user_id"],
                "message_id": message_id,
                "content": content,
            },
        )
        assert response.status_code == 201
        return response.json()

    def snapshot(self, browser, index=0):
        response = browser.get(self.urls[index] + "/api/tasks", params={"limit": 100})
        assert response.status_code == 200
        result = response.json()
        assert result["next_cursor"] is None
        return result

    def close(self):
        self.resources.close()
        for process in self.processes:
            stop_process(process)
        for listener in self.listeners:
            listener.close()
        self.redis.close()


@pytest.fixture
def live_stack(database_url, db_engine, clean_database, tmp_path):
    stack = LiveStack(database_url, db_engine, tmp_path)
    try:
        assert stack.redis.ping()
        for index in range(2):
            stack.start(index)
        assert stack.processes[0].pid != stack.processes[1].pid != os.getpid()
        stack.bot = stack.resources.enter_context(
            httpx.Client(
                base_url=stack.urls[0], headers={"Authorization": "Bearer " + BOT_KEY}, timeout=5
            )
        )
        stack.users = []
        for telegram_id in (970001, 970002):
            response = stack.bot.post(
                "/internal/bot/users",
                json={"telegram_user_id": telegram_id, "private_chat_id": telegram_id},
            )
            assert response.status_code == 200
            stack.users.append(response.json())
        stack.alice = stack.browser(stack.users[0])
        stack.bob = stack.browser(stack.users[1])
        yield stack
    finally:
        stack.close()


def test_two_api_processes_fan_out_owner_changes_and_revoke_sessions(live_stack):
    stack = live_stack
    alice_first = stack.websocket(stack.alice, 0)
    alice_second = stack.websocket(stack.alice, 1)
    bob = stack.websocket(stack.bob, 1)
    started = time.monotonic()
    task = stack.bot_task(message_id=1)
    for websocket in (alice_first, alice_second):
        expect_hint(websocket, "task_created", task, 1)
    elapsed = time.monotonic() - started
    assert elapsed < 2, "Healthy local cross-process event delivery exceeded the 2-second target"
    print(f"Healthy synthetic HTTP commit + delivery to two API sockets: {elapsed:.3f}s")
    for index in range(2):
        snapshot = stack.snapshot(stack.alice, index)
        assert snapshot["revision"] == 1 and snapshot["items"] == [task]

    response = stack.bot.patch(
        f"/internal/bot/tasks/{task['id']}",
        params={"telegram_user_id": stack.users[0]["telegram_user_id"]},
        headers={"If-Match": str(task["version"])},
        json={"status": "in_progress"},
    )
    assert response.status_code == 200
    task = response.json()
    for websocket in (alice_first, alice_second):
        expect_hint(websocket, "task_updated", task, 2)
    assert stack.snapshot(stack.alice, 1)["items"][0]["status"] == "in_progress"

    # The mutation and the first socket live in different API processes.
    response = stack.alice.post(
        stack.urls[1] + "/api/tasks",
        json={"content": "Synthetic dashboard task\nComplete second line."},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    dashboard_task = response.json()
    for websocket in (alice_first, alice_second):
        expect_hint(websocket, "task_created", dashboard_task, 3)
    response = stack.alice.patch(
        stack.urls[1] + f"/api/tasks/{dashboard_task['id']}",
        json={"status": "completed"},
        headers={"If-Match": str(dashboard_task["version"])},
    )
    assert response.status_code == 200
    dashboard_task = response.json()
    for websocket in (alice_first, alice_second):
        expect_hint(websocket, "task_updated", dashboard_task, 4)
    # Telegram reads current state on the next open/list, without updating old messages.
    response = stack.bot.get(
        f"/internal/bot/tasks/{dashboard_task['id']}",
        params={"telegram_user_id": stack.users[0]["telegram_user_id"]},
    )
    assert response.status_code == 200 and response.json()["status"] == "completed"

    response = stack.alice.delete(
        stack.urls[1] + f"/api/tasks/{task['id']}",
        headers={"If-Match": str(task["version"])},
    )
    assert response.status_code == 204
    for websocket in (alice_first, alice_second):
        expect_hint(websocket, "task_deleted", task, 5)
    assert {item["id"] for item in stack.snapshot(stack.alice)["items"]} == {dashboard_task["id"]}
    expect_isolated_heartbeat(bob, 0)
    assert stack.snapshot(stack.bob) == {"items": [], "next_cursor": None, "revision": 0}

    assert stack.alice.post("/api/auth/logout").status_code == 204
    for websocket in (alice_first, alice_second):
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                read_message(websocket, timeout=3)
        assert closed.value.rcvd.code == 4401
    expect_isolated_heartbeat(bob, 0)
    assert stack.alice.get("/api/tasks").status_code == 401

    # Expiry is enforced for an already accepted connection too.
    with stack.factory.begin() as db:
        db.execute(
            update(BrowserSession)
            .where(BrowserSession.owner_id == UUID(stack.users[1]["id"]))
            .values(expires_at=services.utcnow() - timedelta(seconds=1))
        )
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            read_message(bob, timeout=3)
    assert closed.value.rcvd.code == 4401


def test_reconnect_subscriber_restart_and_lost_final_hint_recover_from_postgres(live_stack):
    stack = live_stack
    first = stack.websocket(stack.alice, 0)
    second = stack.websocket(stack.alice, 1)
    removed = stack.bot_task(message_id=1)
    for websocket in (first, second):
        expect_hint(websocket, "task_created", removed, 1)
    second.close()

    created = stack.bot_task(message_id=2)
    response = stack.alice.patch(
        f"/api/tasks/{created['id']}",
        json={"status": "completed"},
        headers={"If-Match": str(created["version"])},
    )
    assert response.status_code == 200
    created = response.json()
    assert (
        stack.alice.delete(
            f"/api/tasks/{removed['id']}", headers={"If-Match": str(removed["version"])}
        ).status_code
        == 204
    )
    second = stack.websocket(stack.alice, 1)
    snapshot = stack.snapshot(stack.alice, 1)
    assert snapshot["revision"] == 4 and snapshot["items"] == [created]
    # Drain the first socket's live mutations before injecting a missed final hint.
    # Competing committed-outbox publishers may reorder hints. The protocol's
    # revisions make order irrelevant; all three changes must still fan out.
    burst = {}
    while len(burst) < 3:
        message = receive_until(first, lambda item: item["type"] in TASK_TYPES)
        burst[message["revision"]] = (message["type"], message["task_id"])
    assert burst == {
        2: ("task_created", created["id"]),
        3: ("task_updated", created["id"]),
        4: ("task_deleted", removed["id"]),
    }

    with stack.factory.begin() as db:
        missing, _ = services.create_task(
            db,
            UUID(stack.users[0]["id"]),
            "Synthetic lost final event",
            "dashboard",
            client_request_id=uuid4(),
        )
        # Fault injection: the DB commit succeeds but this hint has been lost
        # after publication. No later mutation or event is allowed to repair it.
        db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.task_id == missing.id)
            .values(published_at=services.utcnow())
        )
        missing_id = str(missing.id)
    for websocket in (first, second):
        while True:
            message = read_message(websocket, timeout=3)
            if message["type"] in TASK_TYPES:
                # Reopening a socket may catch publication of older committed
                # hints after its ready snapshot. None may announce revision 5.
                assert message["revision"] <= 4
                assert message["task_id"] in {created["id"], removed["id"]}
                continue
            if message["type"] == "heartbeat" and message["revision"] == 5:
                break
    recovered = stack.snapshot(stack.alice, 1)
    assert recovered["revision"] == 5
    assert {item["id"] for item in recovered["items"]} == {created["id"], missing_id}

    # Kill only this test's unique subscribers, never production Redis clients.
    subscribers = [
        client
        for client in stack.redis.client_list()
        if client["name"].startswith(stack.channel + ":subscriber:")
    ]
    assert len(subscribers) == 2
    previous_ids = {client["id"] for client in subscribers}
    for client in subscribers:
        assert stack.redis.client_kill_filter(_id=client["id"]) == 1
    for websocket in (first, second):
        receive_until(websocket, lambda item: item["type"] == "resync" and not item["live"])
        message = receive_until(websocket, lambda item: item["type"] == "resync" and item["live"])
        assert message["revision"] == 5
    new_subscribers = [
        client
        for client in stack.redis.client_list()
        if client["name"].startswith(stack.channel + ":subscriber:")
    ]
    assert len(new_subscribers) == 2
    assert previous_ids.isdisjoint(client["id"] for client in new_subscribers)
    after_restart = stack.bot_task(message_id=3)
    for websocket in (first, second):
        expect_hint(websocket, "task_created", after_restart, 6)

    # An API process restart loses its in-memory connections but not revisions.
    second.close()
    stop_process(stack.processes[1])
    during_restart = stack.bot_task(message_id=4)
    expect_hint(first, "task_created", during_restart, 7)
    stack.start(1)
    stack.websocket(stack.alice, 1)
    assert stack.snapshot(stack.alice, 1) == stack.snapshot(stack.alice, 0)
    assert stack.snapshot(stack.alice, 1)["revision"] == 7


def test_separate_fake_voice_worker_emits_one_task_to_both_api_processes(live_stack, tmp_path):
    stack = live_stack
    first = stack.websocket(stack.alice, 0)
    second = stack.websocket(stack.alice, 1)
    bob = stack.websocket(stack.bob, 1)
    prefix = f"live-voice-{uuid4().hex}:"
    queue = "live-voice-integration"
    transcript = "[Demo transcription] Complete fake voice transcript.\nSecond line 🎙️."
    publisher = Celery("live_voice_test", broker=stack.redis_url, backend=stack.redis_url)
    publisher.conf.update(
        broker_transport_options={"global_keyprefix": prefix},
        result_backend_transport_options={"global_keyprefix": prefix},
        task_serializer="json",
        accept_content=["json"],
    )
    audio = tmp_path / "synthetic-live.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:a",
            "libopus",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    audio_bytes = audio.read_bytes()
    counters = {"downloads": 0}
    adapter_errors = []
    settings = Settings(
        _env_file=None,
        **{
            "database_url": stack.database_url,
            "redis_url": stack.redis_url,
            "bot_api_key": BOT_KEY,
            "app_env": "test",
            "transcription_provider": "fake",
        },
    )

    class Adapter(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                assert self.headers["Authorization"] == "Bearer " + BOT_KEY
                assert self.path.endswith("/audio") and "?" not in self.path
                request_id = UUID(self.path.split("/")[-2])
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert set(payload) == {"lease_token"}
                with stack.factory.begin() as db:
                    context = voice.download_context(
                        db, request_id, UUID(payload["lease_token"]), settings
                    )
                assert context["file_id"] == "synthetic-live-telegram-audio"
                counters["downloads"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "audio/ogg")
                self.send_header("Content-Length", str(len(audio_bytes)))
                self.end_headers()
                self.wfile.write(audio_bytes)
            except Exception as error:
                adapter_errors.append(type(error).__name__)
                self.send_response(500)
                self.end_headers()

    adapter = ThreadingHTTPServer(("127.0.0.1", 0), Adapter)
    thread = threading.Thread(target=adapter.serve_forever, daemon=True)
    thread.start()
    environment = stack.environment | {
        "BOT_BASE_URL": f"http://127.0.0.1:{adapter.server_port}",
        "CELERY_BROKER_KEY_PREFIX": prefix,
        "FAKE_TRANSCRIPT": transcript,
        "VOICE_TEMP_DIR": str(tmp_path / "live-worker-audio"),
    }
    process = None
    try:
        with (tmp_path / "live-worker.log").open("w") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "app.jobs.celery_app:celery_app",
                    "worker",
                    "--pool=solo",
                    "--concurrency=1",
                    "--queues=" + queue,
                    "--hostname=live-voice@%h",
                    "--loglevel=WARNING",
                    "--without-gossip",
                    "--without-mingle",
                    "--without-heartbeat",
                ],
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            probe = publisher.send_task("foundation.probe", args=[str(uuid4())], queue=queue)
            assert probe.get(timeout=25)["worker_pid"] == process.pid
            assert process.pid not in {os.getpid(), *(api.pid for api in stack.processes)}
            probe.forget()
            response = stack.bot.post(
                "/internal/bot/processing-requests",
                json={
                    "telegram_user_id": stack.users[0]["telegram_user_id"],
                    "message_id": 90,
                    "file_id": "synthetic-live-telegram-audio",
                    "duration_seconds": 1,
                    "file_size": len(audio_bytes),
                    "acknowledgement_message_id": 91,
                },
            )
            assert response.status_code == 201
            request_id = response.json()["id"]
            publisher.send_task("voice.transcription", args=[request_id], queue=queue)
            message = receive_until(first, lambda item: item["type"] in TASK_TYPES, timeout=15)
            assert message["type"] == "task_created" and message["revision"] == 1
            expect_hint(second, "task_created", {"id": message["task_id"]}, 1)
            snapshot = stack.snapshot(stack.alice, 1)
            assert snapshot["revision"] == 1 and len(snapshot["items"]) == 1
            task = snapshot["items"][0]
            assert task["id"] == message["task_id"]
            assert task["content"] == transcript
            assert task["source"] == "telegram_voice" and task["status"] == "pending"

            # Repeat delivery in a real queue, fence execution, and prove there
            # was neither a second task/revision nor another download/provider run.
            publisher.send_task("voice.transcription", args=[request_id], queue=queue)
            fence = publisher.send_task("foundation.probe", args=[str(uuid4())], queue=queue)
            assert fence.get(timeout=10)["worker_pid"] == process.pid
            fence.forget()
            with stack.factory() as db:
                assert db.scalar(select(func.count()).select_from(Task)) == 1
                assert db.get(ProcessingRequest, UUID(request_id)).attempts == 1
                assert db.get(User, UUID(stack.users[0]["id"])).task_revision == 1
            expect_isolated_heartbeat(bob, 0)
            assert counters == {"downloads": 1} and not adapter_errors
            assert not list((tmp_path / "live-worker-audio").glob("voice-*"))
    finally:
        stop_process(process)
        adapter.shutdown()
        adapter.server_close()
        thread.join(timeout=2)
        keys = list(stack.redis.scan_iter(prefix + "*"))
        if keys:
            stack.redis.delete(*keys)
        publisher.close()
