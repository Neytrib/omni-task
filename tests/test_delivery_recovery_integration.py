"""Real prefork crash/redelivery and isolated Redis-network fault verification.

Only synthetic users/audio/provider/Telegram transports are used. Killing test
process groups and a per-test TCP proxy never interrupts the running application.
"""

from __future__ import annotations

import gc
import json
import os
import select as io_select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from app import services, voice
from app.jobs.dispatcher import dispatch_once
from app.live_outbox import dispatch_live_once
from app.models import Notification, OutboxEvent, ProcessingRequest, Task
from celery import Celery
from conftest import BOT_KEY
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")


def eventually(predicate, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Synthetic fault-recovery condition did not converge")


class RedisProxy:
    """Drop only this test's TCP sessions while the shared Redis service stays up."""

    def __init__(self, target_url):
        target = urlsplit(target_url)
        assert target.scheme == "redis"
        self.available = threading.Event()
        self.available.set()
        available = self.available

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if not available.is_set():
                    return
                upstream = socket.create_connection((target.hostname, target.port or 6379), 2)
                try:
                    while available.is_set():
                        readable, _, _ = io_select.select([self.request, upstream], [], [], 0.05)
                        for source in readable:
                            payload = source.recv(65536)
                            if not payload:
                                return
                            destination = upstream if source is self.request else self.request
                            destination.sendall(payload)
                except OSError:
                    pass
                finally:
                    upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        credentials = target.netloc.rsplit("@", 1)[0] + "@" if "@" in target.netloc else ""
        netloc = f"{credentials}127.0.0.1:{self.server.server_address[1]}"
        self.url = urlunsplit((target.scheme, netloc, target.path, target.query, ""))

    def close(self):
        self.available.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RecoveryHarness:
    def __init__(self, api, settings, db_engine, database_url, users, bot_headers, directory):
        self.api = api
        self.settings = settings.model_copy(
            update={
                "transcription_provider": "fake",
                "voice_retry_base_seconds": 0,
                "voice_redispatch_seconds": 1,
            }
        )
        # API intake captures the fake provider; never inherit a configured paid provider.
        settings.transcription_provider = "fake"
        self.factory = sessionmaker(db_engine, expire_on_commit=False)
        self.database_url = database_url
        self.users = users
        self.bot_headers = bot_headers
        self.directory = directory
        self.prefix = f"delivery-fault-{uuid4().hex}:"
        self.broker = Redis.from_url(os.environ["TEST_REDIS_URL"], socket_timeout=2)
        self.proxy = RedisProxy(os.environ["TEST_REDIS_URL"])
        self.publisher = Celery("fault_check", broker=self.proxy.url, backend=self.proxy.url)
        self.publisher.conf.update(
            broker_transport_options={
                "global_keyprefix": self.prefix,
                "socket_timeout": 1,
                "socket_connect_timeout": 1,
            },
            result_backend_transport_options={"global_keyprefix": self.prefix},
            task_serializer="json",
            accept_content=["json"],
        )
        self.processes = []
        self.logs = []
        self.downloads = 0
        self.notifications = 0
        self.external_edits = 0
        self.unchanged_edits = 0
        self.notification_started = threading.Event()
        self.release_notification = threading.Event()
        self.release_notification.set()
        self.errors = []
        self.transcript = "  [Demo transcription] Durable complete content.\nSecond line 🎙️.  "
        audio = directory / "synthetic.ogg"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.2",
                "-c:a",
                "libopus",
                str(audio),
            ],
            check=True,
            capture_output=True,
        )
        self.audio = audio.read_bytes()
        harness = self

        class Adapter(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body, content_type="application/json"):
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def do_POST(self):
                try:
                    assert self.headers["Authorization"] == "Bearer " + BOT_KEY
                    identifier = UUID(self.path.split("/")[-2])
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    assert set(body) == {"lease_token"}
                    lease = UUID(body["lease_token"])
                    if self.path.endswith("/audio"):
                        with harness.factory.begin() as db:
                            voice.download_context(db, identifier, lease, harness.settings)
                        harness.downloads += 1
                        self.reply(200, harness.audio, "audio/ogg")
                    elif self.path.endswith("/notify"):
                        with harness.factory.begin() as db:
                            context = voice.notification_context(
                                db, identifier, lease, harness.settings
                            )
                        assert context["state"] == "succeeded"
                        harness.notifications += 1
                        if harness.external_edits:
                            harness.unchanged_edits += 1
                        else:
                            harness.external_edits += 1
                        # The fake external edit can succeed before its HTTP reply is lost.
                        harness.notification_started.set()
                        if not harness.release_notification.wait(40):
                            raise AssertionError("Synthetic notification barrier timed out")
                        self.reply(200, b'{"outcome":"sent","message_id":710}')
                    else:
                        raise AssertionError("Unexpected synthetic adapter path")
                except Exception as error:
                    harness.errors.append(type(error).__name__)
                    self.reply(500, b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Adapter)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "DATABASE_URL": database_url,
                "REDIS_URL": self.proxy.url,
                "BOT_API_KEY": BOT_KEY,
                "BOT_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
                "APP_ENV": "test",
                "CELERY_BROKER_KEY_PREFIX": self.prefix,
                "TRANSCRIPTION_PROVIDER": "fake",
                "FAKE_TRANSCRIPT": self.transcript,
                "OPENAI_API_KEY": "",
                "ALLOW_PAID_TRANSCRIPTION": "false",
                "VOICE_RETRY_BASE_SECONDS": "0",
                "VOICE_REDISPATCH_SECONDS": "1",
                "VOICE_TEMP_DIR": str(directory / "audio"),
                "PYTHONPATH": str(Path(__file__).parent)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            }
        )

    def start(self, *, supervisor=False, cached_barrier=False):
        environment = dict(self.environment)
        if supervisor:
            command = [sys.executable, "-m", "app.jobs.worker"]
        else:
            environment["TEST_DELIVERY_EVENTS"] = str(self.directory / "deliveries.jsonl")
            if cached_barrier:
                environment["TEST_CACHED_TRANSCRIPT_BARRIER"] = str(
                    self.directory / "cached.marker"
                )
            command = [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.jobs.celery_app:celery_app",
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                "--queues=transcription",
                "--hostname=fault-transcription@%h",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--include=fault_worker_hooks",
            ]
        log = (self.directory / f"worker-{len(self.logs)}.log").open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            command, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        self.processes.append(process)
        evidence = self.probe("foundation" if supervisor else "transcription")
        assert evidence["worker_pid"] not in {process.pid, os.getpid()}
        if supervisor:
            # The two consumers start independently; foundation readiness alone is insufficient.
            second = self.probe("transcription")
            assert second["worker_pid"] not in {process.pid, evidence["worker_pid"], os.getpid()}
        return process

    def probe(self, queue):
        result = self.publisher.send_task("foundation.probe", args=[str(uuid4())], queue=queue)
        try:
            return result.get(timeout=30)
        finally:
            result.forget()
            # Celery's fulfilled promise can retain a result cycle after forget().
            # Its destructor still UNSUBSCRIBEs: release it before fault injection.
            del result
            gc.collect()

    def accept(self, message_id=700):
        response = self.api.post(
            "/internal/bot/processing-requests",
            headers=self.bot_headers,
            json={
                "telegram_user_id": self.users[0]["telegram_user_id"],
                "message_id": message_id,
                "file_id": "synthetic-crash-test-audio",
                "duration_seconds": 1,
                "file_size": len(self.audio),
                "acknowledgement_message_id": 710,
            },
        )
        assert response.status_code == 201
        return UUID(response.json()["id"])

    def publish(self, name, *, args, queue, **options):
        assert name in {"voice.transcription", "voice.notification"}
        assert len(args) == 1 and str(UUID(args[0])) == args[0]
        return self.publisher.send_task(name, args=args, queue=queue, **options)

    def drain_until(self, predicate, *, timeout=30):
        def check():
            dispatch_once(self.factory, self.settings, publisher=self.publish)
            with self.factory() as db:
                return predicate(db)

        eventually(check, timeout=timeout)

    def deliveries(self):
        path = self.directory / "deliveries.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    @staticmethod
    def kill(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def close(self):
        self.release_notification.set()
        self.proxy.available.set()
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.kill(process)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # All result/client cleanup must precede destruction of the private transport.
        gc.collect()
        self.publisher.backend.result_consumer.stop()
        self.publisher.backend.client.close()
        self.publisher.close()
        self.proxy.close()
        for log in self.logs:
            log.close()
        keys = list(self.broker.scan_iter(self.prefix + "*"))
        if keys:
            self.broker.delete(*keys)
        self.broker.close()


@pytest.fixture
def recovery(api, settings, db_engine, database_url, users, bot_headers, tmp_path):
    harness = RecoveryHarness(api, settings, db_engine, database_url, users, bot_headers, tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def test_prefork_child_sigkill_redelivers_and_recovers_cached_transcript(recovery):
    harness = recovery
    harness.start(cached_barrier=True)
    request_id = harness.accept()
    assert dispatch_once(harness.factory, harness.settings, publisher=harness.publish) == 1
    assert not harness.publisher.backend.result_consumer.subscribed_to
    marker = harness.directory / "cached.marker"
    eventually(marker.exists)
    child_pid = int(marker.read_text())
    with harness.factory() as db:
        request = db.get(ProcessingRequest, request_id)
        assert request.cached_transcript == harness.transcript
        assert request.attempts == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 0
    os.kill(child_pid, signal.SIGKILL)
    eventually(
        lambda: any(
            item["phase"] == "finish"
            and item["task_name"] == "voice.transcription"
            and item["redelivered"]
            for item in harness.deliveries()
        ),
        timeout=30,
    )
    marker.with_suffix(".release").touch()
    with harness.factory.begin() as db:
        request = db.get(ProcessingRequest, request_id)
        # Advance only the synthetic DB lease deadline; do not pretend 240 wall seconds elapsed.
        request.lease_expires_at = services.utcnow() - timedelta(seconds=1)
    harness.drain_until(lambda db: db.get(ProcessingRequest, request_id).state == "succeeded")
    with harness.factory() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(Task.content)) == harness.transcript
        assert db.get(ProcessingRequest, request_id).attempts == 1
        assert db.scalar(select(Notification.state)) == "pending"
    assert harness.downloads == 1
    assert not harness.errors


def test_production_supervisor_restart_preserves_task_and_retries_only_notification(recovery):
    harness = recovery
    harness.release_notification.clear()
    process = harness.start(supervisor=True)
    hostname = socket.gethostname()
    queues = harness.publisher.control.inspect(timeout=3).active_queues()
    assert {item["name"] for item in queues[f"transcription@{hostname}"]} == {"transcription"}
    assert {item["name"] for item in queues[f"notifications@{hostname}"]} == {
        "notifications",
        "foundation",
    }
    health = subprocess.run(
        [sys.executable, "-m", "app.jobs.worker", "--health"],
        env=harness.environment,
        capture_output=True,
        timeout=12,
    )
    assert health.returncode == 0
    request_id = harness.accept()
    harness.drain_until(lambda db: db.scalar(select(Notification.state)) == "delivering")
    assert harness.notification_started.wait(5)
    with harness.factory() as db:
        assert db.get(ProcessingRequest, request_id).state == "succeeded"
        assert db.scalar(select(func.count()).select_from(Task)) == 1
    harness.kill(process)
    harness.release_notification.set()
    with harness.factory.begin() as db:
        delivery = db.scalar(select(Notification))
        delivery.lease_expires_at = services.utcnow() - timedelta(seconds=1)
    harness.start(supervisor=True)
    harness.drain_until(lambda db: db.scalar(select(Notification.state)) == "succeeded")
    with harness.factory() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(Task.content)) == harness.transcript
        assert db.get(ProcessingRequest, request_id).attempts == 1
        assert db.scalar(select(Notification.attempts)) == 2
    assert harness.downloads == 1
    assert harness.notifications == 2
    assert harness.external_edits == 1 and harness.unchanged_edits == 1
    assert not harness.errors


def test_isolated_redis_outage_preserves_committed_work_and_recovers_consumers(recovery):
    harness = recovery
    process = harness.start(supervisor=True)
    harness.proxy.available.clear()
    time.sleep(0.2)  # Let the proxy close existing connections, not the shared Redis server.
    # Deferred result destructors must not need a proxy that this fault has disconnected.
    gc.collect()
    request_id = harness.accept()
    started = time.monotonic()
    assert dispatch_once(harness.factory, harness.settings, publisher=harness.publish) == 0
    assert time.monotonic() - started < 5
    assert not harness.publisher.backend.result_consumer.subscribed_to
    with harness.factory() as db:
        assert db.get(ProcessingRequest, request_id).state == "queued"
        assert db.scalar(select(func.count()).select_from(Task)) == 0
    harness.proxy.available.set()
    harness.drain_until(lambda db: db.scalar(select(Notification.state)) == "succeeded", timeout=40)
    assert process.poll() is None
    with harness.factory() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.get(ProcessingRequest, request_id).attempts == 1
    assert harness.downloads == 1 and harness.notifications == 1
    assert not harness.errors


def test_real_publish_ack_loss_replays_same_committed_hint_without_duplicate_task(recovery):
    harness = recovery
    response = harness.api.post(
        "/internal/bot/tasks",
        headers=harness.bot_headers,
        json={
            "telegram_user_id": harness.users[0]["telegram_user_id"],
            "message_id": 701,
            "content": "Synthetic durable outbox task",
        },
    )
    assert response.status_code == 201
    settings = harness.settings.model_copy(update={"live_channel": harness.prefix + "live"})
    with harness.broker.pubsub() as subscriber:
        subscriber.subscribe(settings.live_channel)
        assert subscriber.get_message(timeout=2)["type"] == "subscribe"

        def ambiguous_publish(channel, payload):
            harness.broker.publish(channel, payload)
            raise RedisConnectionError("Synthetic lost publish acknowledgement")

        with pytest.raises(RedisError):
            dispatch_live_once(harness.factory, settings, ambiguous_publish)
        first = subscriber.get_message(timeout=2)
        assert first["type"] == "message"
        with harness.factory() as db:
            assert db.scalar(select(OutboxEvent.published_at)) is None
            assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert dispatch_live_once(harness.factory, settings, harness.broker.publish) == 1
        repeated = subscriber.get_message(timeout=2)
        assert repeated["data"] == first["data"]
        hint = json.loads(repeated["data"])
        assert set(hint) == {"type", "owner_id", "task_id", "revision"}
        assert hint["task_id"] == response.json()["id"] and hint["revision"] == 1
        with harness.factory() as db:
            assert db.scalar(select(OutboxEvent.published_at)) is not None
            assert db.scalar(select(func.count()).select_from(Task)) == 1
