"""Restore synthetic data into a new database and recover jobs from an empty Redis.

Creates and removes only a randomly named private Compose project. No .env,
real Telegram/OpenAI credentials, published ports, or existing volumes are used.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from backup import ROOT, Compose, backup
from restore import restore

SEED = r"""
import json
from app import auth, services, voice
from app.config import Settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

settings = Settings(voice_dispatch_enabled=False, live_enabled=False)
factory = sessionmaker(create_engine(settings.database_url), expire_on_commit=False)
content = "  Complete restore content.\n" + "Հայերեն 🎙️\n" * 1000 + "Final line preserved.  "
with factory.begin() as db:
    user = services.ensure_user(db, 930001, 930001)
    services.ensure_user(db, 930002, 930002)
    task, _ = services.create_task(db, user.id, content, "telegram_text",
        bot_identity=settings.bot_identity, message_id=101)
    services.change_status(db, user.id, task.id, "in_progress", task.version)
    deleted, _ = services.create_task(db, user.id, "Deleted synthetic content", "telegram_text",
        bot_identity=settings.bot_identity, message_id=102)
    services.delete_task(db, user.id, deleted.id, deleted.version)
with factory.begin() as db:
    # Voice admission takes its global lock before taking an owner lock.
    for message in (103, 104):
        request, _ = services.create_processing_request(db, user.id, "synthetic-restore-audio",
            settings.bot_identity, message, duration_seconds=1, file_size=None,
            ack_message_id=message + 100, provider_name="fake", settings=settings)
        if message == 103:
            queued = str(request.id)
        else:
            saved = str(request.id)
            claim = voice.claim_transcription(db, request.id, settings)
            assert voice.save_transcript(db, request.id, claim["lease_token"],
                "Already transcribed before backup.", settings)
            voice.finish_transcription(db, request.id, claim["lease_token"], settings)
    link = auth.issue_login_link(db, user, settings)
    cookie, session = auth.exchange_link(db, link["token"], settings)
    current = auth.authenticate_session(db, cookie)
    seed = {"cookie": cookie, "csrf": current.csrf_token, "used_link": link["token"],
        "task_id": str(task.id), "content": content, "queued": queued, "saved": saved}
# Only a private captured pipe receives these synthetic session credentials.
print(json.dumps(seed))
"""

VERIFY = r"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from app import voice
from app.config import Settings
from app.jobs.celery_app import celery_app
from app.jobs.dispatcher import dispatch_once
from app.main import create_app
from app.models import Notification, ProcessingRequest, Task

seed = json.load(sys.stdin)
settings = Settings(voice_dispatch_enabled=False, live_enabled=False)
factory = sessionmaker(create_engine(settings.database_url), expire_on_commit=False)
headers = {"Authorization": "Bearer " + settings.bot_api_key.get_secret_value()}
origin = settings.dashboard_origin
assert not settings.allow_paid_transcription and not settings.openai_api_key
with Redis.from_url(settings.redis_url) as redis:
    assert redis.dbsize() == 0, "Restore test must start with empty private Redis"
with TestClient(create_app(settings), base_url=origin) as api:
    api.cookies.set("omni_session", seed["cookie"])
    current = api.get("/api/session")
    assert current.status_code == 200 and current.json()["csrf_token"] == seed["csrf"]
    loaded = api.get("/api/tasks/" + seed["task_id"])
    assert loaded.status_code == 200
    assert loaded.json()["content"] == seed["content"]
    assert loaded.json()["status"] == "in_progress"
    assert api.patch("/api/tasks/" + seed["task_id"],
        headers={"Origin": origin, "If-Match": str(loaded.json()["version"])},
        json={"status": "completed"}).status_code == 403
    rejected = api.post("/api/auth/exchange", headers={"Origin": origin},
        json={"token": seed["used_link"]})
    assert rejected.status_code == 401
    # Bot-only calls never accept the browser cookie.
    api.cookies.clear()
    duplicate = api.post("/internal/bot/tasks", headers=headers, json={
        "telegram_user_id": 930001, "message_id": 101, "content": seed["content"]})
    assert duplicate.status_code == 200 and duplicate.json()["id"] == seed["task_id"]
    deleted = api.post("/internal/bot/tasks", headers=headers, json={
        "telegram_user_id": 930001, "message_id": 102, "content": "Deleted synthetic content"})
    assert deleted.status_code == 410
    assert api.get("/internal/bot/tasks/" + seed["task_id"], headers=headers,
        params={"telegram_user_id": 930002}).status_code == 404

counters = {"downloads": 0, "notifications": 0}
errors = []
with tempfile.TemporaryDirectory(prefix="restore-media-") as temporary:
    directory = Path(temporary)
    audio = directory / "synthetic.ogg"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.5", "-c:a", "libopus", str(audio)],
        check=True, capture_output=True, timeout=10)
    audio_bytes = audio.read_bytes()

    class Adapter(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            try:
                assert self.headers["Authorization"] == headers["Authorization"]
                request_id = UUID(self.path.split("/")[-2])
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                lease = UUID(data["lease_token"])
                if self.path.endswith("/audio"):
                    with factory.begin() as db:
                        voice.download_context(db, request_id, lease, settings)
                    assert str(request_id) == seed["queued"]
                    counters["downloads"] += 1
                    body, content_type = audio_bytes, "audio/ogg"
                elif self.path.endswith("/notify"):
                    with factory.begin() as db:
                        state = voice.notification_context(db, request_id, lease, settings)
                    assert state["state"] == "succeeded"
                    counters["notifications"] += 1
                    body, content_type = b'{"outcome":"sent","message_id":777}', "application/json"
                else:
                    raise AssertionError("Unexpected synthetic adapter endpoint")
                self.send_response(200)
            except Exception as error:
                errors.append(type(error).__name__)
                body, content_type = b"{}", "application/json"
                self.send_response(500)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Adapter)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = os.environ.copy()
    environment.update(BOT_BASE_URL=f"http://127.0.0.1:{server.server_port}",
        TRANSCRIPTION_PROVIDER="fake", FAKE_TRANSCRIPT="Complete restored queued transcript.",
        OPENAI_API_KEY="", ALLOW_PAID_TRANSCRIPTION="false",
        VOICE_TEMP_DIR=str(directory / "audio"))
    worker = subprocess.Popen([sys.executable, "-m", "celery", "-A",
        "app.jobs.celery_app:celery_app", "worker", "--loglevel=WARNING", "--concurrency=2",
        "--queues=transcription,notifications,foundation", "--without-gossip", "--without-mingle"],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        probe = celery_app.send_task("foundation.probe", args=[str(uuid4())], queue="foundation")
        assert probe.get(timeout=30)["worker_pid"] not in {os.getpid(), worker.pid}
        probe.forget()
        deadline = time.monotonic() + 30
        while True:
            assert worker.poll() is None
            dispatch_once(factory, settings)
            with factory() as db:
                states = list(db.scalars(select(Notification.state)))
            if states == ["succeeded", "succeeded"]:
                break
            assert time.monotonic() < deadline, "Restored jobs did not recover"
            time.sleep(0.1)
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Task)) == 3
            stored = db.scalar(select(Task.content).where(Task.id == UUID(seed["task_id"])))
            assert stored == seed["content"]
            assert db.get(ProcessingRequest, UUID(seed["saved"])).attempts == 1
            assert db.get(ProcessingRequest, UUID(seed["queued"])).state == "succeeded"
        assert counters == {"downloads": 1, "notifications": 2} and not errors
    finally:
        worker.terminate()
        try: worker.wait(timeout=15)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
        server.shutdown()
        server.server_close()
print(json.dumps({"status": "passed", "full_content": "preserved", "sessions_csrf": "preserved",
    "used_login": "rejected", "source_receipts": "preserved", "owner_isolation": "passed",
    "empty_redis": "recovered", "queued_voice": "succeeded", "saved_notification": "succeeded",
    "separate_prefork_worker": "passed", "paid_calls": "none"}))
"""


def main():
    project = "omni-restore-test-" + uuid4().hex[:12]
    root = ROOT / "tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="restore-check-", dir=root) as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        envfile = directory / "fixture.env"
        envfile.write_text(
            "\n".join(
                [
                    "POSTGRES_USER=omni_restore",
                    "POSTGRES_DB=omni_restore_source_test",
                    "POSTGRES_PASSWORD=" + secrets.token_urlsafe(32),
                    "BOT_API_KEY=" + secrets.token_urlsafe(48),
                    "BOT_IDENTITY=" + project,
                    "APP_ENV=test",
                    "DASHBOARD_ORIGIN=http://127.0.0.1:8080",
                    "COOKIE_SECURE=false",
                    "TRANSCRIPTION_PROVIDER=fake",
                    "TELEGRAM_BOT_TOKEN=",
                    "OPENAI_API_KEY=",
                    "ALLOW_PAID_TRANSCRIPTION=false",
                    "LIVE_CHANNEL=" + project + ":live",
                    "",
                ]
            )
        )
        envfile.chmod(0o600)
        args = argparse.Namespace(
            env_file=envfile,
            project_name=project,
            file=[ROOT / "docker-compose.yml"],
            timeout_seconds=150,
        )
        compose = Compose(args)
        config = json.loads(compose.run(["config", "--format", "json"]))
        for service in config["services"].values():
            service.pop("ports", None)
            service.pop("build", None)
        for resource in ("volumes", "networks"):
            for item in config[resource].values():
                assert item["name"].startswith(project + "_") and not item.get("external")
        fixture = directory / "compose.json"
        fixture.write_text(json.dumps(config))
        fixture.chmod(0o600)
        args.file = [fixture]
        compose = Compose(args)
        try:
            compose.run(
                [
                    "up",
                    "--detach",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "60",
                    "postgres",
                    "redis",
                ]
            )
            compose.run(["run", "--rm", "-T", "--no-deps", "migrate"])
            seed = compose.run(["run", "--rm", "-T", "--no-deps", "api", "python", "-c", SEED])
            json.loads(seed)
            bundle = backup(compose, directory / "backups")
            target = "omni_restore_target_test"
            restore(compose, bundle, target)
            # Prove a second attempt is refused without changing the restored target.
            try:
                restore(compose, bundle, target)
            except ValueError:
                pass
            else:
                raise AssertionError("Existing restore target was not refused")
            config["services"]["api"]["environment"]["DATABASE_URL"] = (
                config["services"]["api"]["environment"]["DATABASE_URL"].rsplit("/", 1)[0]
                + "/"
                + target
            )
            fixture.write_text(json.dumps(config))
            evidence = json.loads(
                compose.run(
                    ["run", "--rm", "-T", "--no-deps", "api", "python", "-c", VERIFY],
                    input_bytes=seed,
                )
            )
        finally:
            assert re.fullmatch(r"omni-restore-test-[a-f0-9]{12}", project)
            compose.run(["down", "--volumes", "--remove-orphans", "--timeout", "15"])
        print(json.dumps({**evidence, "cleanup": "private project removed"}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, AssertionError, subprocess.TimeoutExpired):
        raise SystemExit(
            "Isolated restore verification failed; private diagnostics suppressed."
        ) from None
