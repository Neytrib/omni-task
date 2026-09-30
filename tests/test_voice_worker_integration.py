"""Real Redis + a separate Celery process; Telegram and paid providers stay synthetic."""

import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4

from app import voice
from app.jobs.dispatcher import dispatch_once
from app.models import Notification, ProcessingRequest, Task
from celery import Celery
from conftest import BOT_KEY
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def test_redis_separate_worker_restart_retry_and_duplicate_delivery(
    api, settings, db_engine, database_url, bot_headers, users, tmp_path
):
    redis_url = os.environ["TEST_REDIS_URL"]
    prefix = f"voice-test-{uuid4().hex}:"
    queue = "voice-integration"
    broker = Redis.from_url(redis_url)
    assert broker.ping()
    publisher = Celery("voice_integration", broker=redis_url, backend=redis_url)
    publisher.conf.update(
        broker_transport_options={"global_keyprefix": prefix},
        result_backend_transport_options={"global_keyprefix": prefix},
        task_serializer="json",
        accept_content=["json"],
    )
    factory = sessionmaker(db_engine, expire_on_commit=False)
    transcript = "  [Demo transcription] First complete line.\nSecond line with emoji 🎙️.  "
    settings.transcription_provider = "fake"
    settings.voice_retry_base_seconds = 0
    settings.voice_redispatch_seconds = 1
    audio = tmp_path / "synthetic.ogg"
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
    counters = {"downloads": 0, "notifications": 0}
    errors = []

    class Adapter(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, content, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            try:
                assert self.headers["Authorization"] == "Bearer " + BOT_KEY
                assert "?" not in self.path
                identifier = UUID(self.path.split("/")[-2])
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert set(payload) == {"lease_token"}
                lease = UUID(payload["lease_token"])
                if self.path.endswith("/audio"):
                    with factory.begin() as db:
                        context = voice.download_context(db, identifier, lease, settings)
                    assert context["file_id"] == "synthetic-telegram-ogg-file"
                    counters["downloads"] += 1
                    self.reply(200, audio_bytes, "audio/ogg")
                elif self.path.endswith("/notify"):
                    with factory.begin() as db:
                        context = voice.notification_context(db, identifier, lease, settings)
                    assert context["state"] == "succeeded"
                    assert context["task"]["status"] == "pending"
                    counters["notifications"] += 1
                    if counters["notifications"] == 1:
                        self.reply(503, b'{"error":"synthetic_temporary_unavailable"}')
                    else:
                        self.reply(200, b'{"outcome":"sent","message_id":710}')
                else:
                    self.reply(404, b"{}")
            except Exception as error:
                errors.append(type(error).__name__)
                self.reply(500, b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "BOT_API_KEY": BOT_KEY,
            "BOT_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "APP_ENV": "test",
            "CELERY_BROKER_KEY_PREFIX": prefix,
            "TRANSCRIPTION_PROVIDER": "fake",
            "FAKE_TRANSCRIPT": transcript,
            "OPENAI_API_KEY": "",
            "ALLOW_PAID_TRANSCRIPTION": "false",
            "VOICE_RETRY_BASE_SECONDS": "0",
            "VOICE_REDISPATCH_SECONDS": "1",
            "VOICE_TEMP_DIR": str(tmp_path / "worker-audio"),
        }
    )
    process = None
    log = (tmp_path / "synthetic-worker.log").open("w+")

    def publish(name, *, args, **_):
        assert name in {"voice.transcription", "voice.notification"}
        assert len(args) == 1 and str(UUID(args[0])) == args[0]
        publisher.send_task(name, args=args, queue=queue, retry=False)

    def drain_until(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert process.poll() is None
            dispatch_once(factory, settings, publisher=publish)
            with factory() as db:
                if predicate(db):
                    return
            time.sleep(0.1)
        raise AssertionError("Separate worker did not finish the synthetic voice flow in time")

    def start_worker():
        return subprocess.Popen(
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
                "--hostname=voice-integration@%h",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        process = start_worker()
        probe = publisher.send_task("foundation.probe", args=[str(uuid4())], queue=queue)
        evidence = probe.get(timeout=25)
        assert evidence["worker_pid"] == process.pid != os.getpid()
        probe.forget()
        # A SIGSTOP'ed worker can still have a pending BRPOP at Redis; Redis would
        # remove a queued message into that socket. Stop it cleanly before inspecting
        # broker contents, then prove a fresh worker process recovers accepted work.
        process.terminate()
        process.wait(timeout=10)
        process = None
        response = api.post(
            "/internal/bot/processing-requests",
            headers=bot_headers,
            json={
                "telegram_user_id": users[0]["telegram_user_id"],
                "message_id": 700,
                "file_id": "synthetic-telegram-ogg-file",
                "duration_seconds": 1,
                "file_size": len(audio_bytes),
                "acknowledgement_message_id": 710,
            },
        )
        assert response.status_code == 201, response.text
        request_id = UUID(response.json()["id"])
        assert dispatch_once(factory, settings, publisher=publish) == 1
        envelope = json.loads(broker.lindex(prefix + queue, 0))
        payload = json.loads(base64.b64decode(envelope["body"]))
        assert payload[0] == [str(request_id)]
        assert transcript not in json.dumps(envelope)
        assert "synthetic-telegram-ogg-file" not in json.dumps(envelope)
        with factory() as db:
            assert db.get(ProcessingRequest, request_id).state == "queued"
            assert db.scalar(select(func.count()).select_from(Task)) == 0
        assert (
            api.get(
                "/internal/bot/tasks",
                headers=bot_headers,
                params={"telegram_user_id": users[0]["telegram_user_id"]},
            ).status_code
            == 200
        )
        publish("voice.transcription", args=[str(request_id)])
        process = start_worker()
        drain_until(lambda db: db.scalar(select(Notification.state)) == "succeeded")
        with factory() as db:
            request = db.get(ProcessingRequest, request_id)
            delivery = db.scalar(select(Notification))
            task = db.scalar(select(Task))
            assert request.state == "succeeded" and request.attempts == 1
            assert request.cached_transcript is None
            assert task.content == transcript and task.status == "pending"
            assert task.source == "telegram_voice"
            assert delivery.attempts == 2 and delivery.sent_message_id == 710
            notification_id = str(delivery.id)
        publish("voice.transcription", args=[str(request_id)])
        publish("voice.notification", args=[notification_id])
        # A probe on the same serial consumer fences both duplicate deliveries.
        fence = publisher.send_task("foundation.probe", args=[str(uuid4())], queue=queue)
        assert fence.get(timeout=10)["worker_pid"] == process.pid
        fence.forget()
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Task)) == 1
            assert db.get(ProcessingRequest, request_id).attempts == 1
            assert db.scalar(select(Notification.attempts)) == 2
        assert counters == {"downloads": 1, "notifications": 2}
        assert not errors
        assert not list((tmp_path / "worker-audio").glob("voice-*"))
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        log.close()
        # Only this test's prefixed keys; never flush a database or touch production queues.
        keys = list(broker.scan_iter(prefix + "*"))
        if keys:
            broker.delete(*keys)
        broker.close()
        publisher.close()
