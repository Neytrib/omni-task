"""Verify current images in a fresh private Compose project, then remove only that project.

No application credentials are read from .env. Parent application variables are
removed; generated fixture credentials stay in private temporary files and pipes.
This script never stops, recreates, or resets the user's running services/volumes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

SEED = r"""
import json
import os
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from redis import Redis
from app.jobs.celery_app import celery_app

origin = os.environ["DASHBOARD_ORIGIN"]
headers = {"Authorization": "Bearer " + os.environ["BOT_API_KEY"]}
content = "  Synthetic restart content.\nComplete Unicode 🎙️ preserved.  "
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5) as bot:
    for identity in (920001, 920002):
        response = bot.post("/internal/bot/users", headers=headers, json={
            "telegram_user_id": identity, "private_chat_id": identity,
        })
        response.raise_for_status()
    response = bot.post("/internal/bot/tasks", headers=headers, json={
        "telegram_user_id": 920001, "message_id": 42, "content": content,
    })
    assert response.status_code == 201
    task = response.json()
    link = bot.post("/internal/bot/login-links", headers=headers, json={
        "telegram_user_id": 920001,
    })
    link.raise_for_status()
    token = parse_qs(urlsplit(link.json()["url"]).fragment)["token"][0]
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5) as browser:
    response = browser.post("/api/auth/exchange", headers={"Origin": origin}, json={"token": token})
    response.raise_for_status()
    session = browser.get("/api/session")
    session.raise_for_status()
    credential = browser.cookies.get("omni_session")
    assert credential
with Redis.from_url(os.environ["REDIS_URL"]) as redis:
    redis.set("isolated-recovery-marker", "synthetic-marker")
nonce = str(uuid4())
probe = celery_app.send_task("foundation.probe", args=[nonce], queue="foundation")
# The worker is stopped: this harmless job must survive Redis container recreation.
# The caller captures this privately; credentials are never printed to the terminal.
print(json.dumps({"task": task, "cookie": credential, "csrf": session.json()["csrf_token"],
                  "probe_id": probe.id, "probe_nonce": nonce}))
"""

VERIFY = r"""
import json
import os
import sys
import time

import httpx
from redis import Redis

from app.jobs.celery_app import celery_app

seed = json.load(sys.stdin)
headers = {"Authorization": "Bearer " + os.environ["BOT_API_KEY"]}
origin = os.environ["DASHBOARD_ORIGIN"]
deadline = time.monotonic() + 25
while True:
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=3) as browser:
            browser.cookies.set("omni_session", seed["cookie"])
            response = browser.get("/api/session")
            response.raise_for_status()
            assert response.json()["csrf_token"] == seed["csrf"]
            response = browser.get("/api/tasks")
            response.raise_for_status()
            assert response.json()["items"] == [seed["task"]]
        with Redis.from_url(os.environ["REDIS_URL"], socket_timeout=2) as redis:
            assert redis.get("isolated-recovery-marker") == b"synthetic-marker"
        break
    except (httpx.HTTPError, OSError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.2)
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5) as bot:
    repeated = bot.post("/internal/bot/tasks", headers=headers, json={
        "telegram_user_id": 920001, "message_id": 42, "content": seed["task"]["content"],
    })
    assert repeated.status_code == 200 and repeated.json() == seed["task"]
    forbidden = bot.get("/internal/bot/tasks/" + seed["task"]["id"], headers=headers,
        params={"telegram_user_id": 920002})
    assert forbidden.status_code == 404
    assert bot.get("/api/tasks").status_code == 401
probe = celery_app.AsyncResult(seed["probe_id"])
assert probe.get(timeout=25)["nonce"] == seed["probe_nonce"]
probe.forget()
print(json.dumps({"tasks": "preserved", "sessions": "preserved", "deduplication": "preserved",
                  "redis_aof_marker": "preserved", "queued_celery_job": "preserved",
                  "second_user_isolation": "passed"}))
"""


def command(args, *, environment, phase, input_text=None):
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=150,
    )
    if result.returncode:
        # Docker configuration/output can contain generated credentials; do not echo it.
        raise RuntimeError(f"Isolated recovery check failed at {phase} (exit {result.returncode}).")
    return result.stdout


def main() -> None:
    project = "omni-recovery-test-" + uuid4().hex[:12]
    environment = os.environ.copy()
    compose_source = (ROOT / "docker-compose.yml").read_text()
    names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    names.update(re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M))
    names.update({"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"})
    for name in names:
        environment.pop(name, None)
    environment["COMPOSE_DISABLE_ENV_FILE"] = "true"
    root = ROOT / "tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isolated-recovery-", dir=root) as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        envfile = directory / "fixture.env"
        envfile.write_text(
            "\n".join(
                [
                    "POSTGRES_USER=omni_recovery",
                    "POSTGRES_DB=omni_recovery_test",
                    "POSTGRES_PASSWORD=" + secrets.token_urlsafe(32),
                    "BOT_API_KEY=" + secrets.token_urlsafe(48),
                    "BOT_IDENTITY=" + project,
                    "APP_ENV=development",
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
        os.chmod(envfile, 0o600)
        initial = [
            "docker",
            "compose",
            "--env-file",
            str(envfile),
            "--project-name",
            project,
            "--file",
            str(ROOT / "docker-compose.yml"),
        ]
        configuration = json.loads(
            command(
                initial + ["config", "--format", "json"],
                environment=environment,
                phase="configuration",
            )
        )
        assert configuration["name"] == project
        for service in configuration["services"].values():
            service.pop("ports", None)
            service.pop("build", None)
        for resource in ("volumes", "networks"):
            for item in configuration[resource].values():
                assert item["name"].startswith(project + "_") and not item.get("external")
        assert configuration["services"]["bot"]["environment"]["TELEGRAM_BOT_TOKEN"] == ""
        worker_env = configuration["services"]["worker"]["environment"]
        assert (
            worker_env["OPENAI_API_KEY"] == "" and worker_env["ALLOW_PAID_TRANSCRIPTION"] == "false"
        )
        assert worker_env["TRANSCRIPTION_PROVIDER"] == "fake"
        fixture = directory / "compose.json"
        fixture.write_text(json.dumps(configuration))
        os.chmod(fixture, 0o600)
        compose = ["docker", "compose", "--project-name", project, "--file", str(fixture)]
        created = False
        evidence = {}
        try:
            created = True
            command(
                compose
                + [
                    "up",
                    "--detach",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "90",
                ],
                environment=environment,
                phase="fresh private startup",
            )
            for service in ("api", "bot", "worker", "frontend", "postgres", "redis"):
                identifier = command(
                    compose + ["ps", "--quiet", service],
                    environment=environment,
                    phase="service inventory",
                ).strip()
                assert identifier
                inspected = json.loads(
                    command(
                        ["docker", "inspect", identifier],
                        environment=environment,
                        phase="port boundary",
                    )
                )[0]
                assert not inspected["HostConfig"]["PortBindings"]
                assert inspected["State"]["Health"]["Status"] == "healthy"
            command(
                compose + ["stop", "--timeout", "30", "worker"],
                environment=environment,
                phase="pause isolated consumers before queuing the harmless probe",
            )
            seed = command(
                compose + ["exec", "-T", "api", "python", "-c", SEED],
                environment=environment,
                phase="synthetic seed",
            )
            json.loads(seed)  # Validate before carrying opaque credentials through a private pipe.
            before = {
                service: command(
                    compose + ["ps", "--quiet", service],
                    environment=environment,
                    phase="initial container IDs",
                ).strip()
                for service in ("postgres", "redis")
            }
            command(
                compose
                + [
                    "up",
                    "--detach",
                    "--no-deps",
                    "--no-build",
                    "--pull",
                    "never",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "90",
                    "postgres",
                    "redis",
                ],
                environment=environment,
                phase="isolated data-container recreation",
            )
            for service, previous in before.items():
                current = command(
                    compose + ["ps", "--quiet", service],
                    environment=environment,
                    phase="replacement container IDs",
                ).strip()
                assert current and current != previous
            command(
                compose
                + [
                    "up",
                    "--detach",
                    "--no-deps",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "90",
                    "worker",
                ],
                environment=environment,
                phase="resume isolated worker consumers",
            )
            evidence = json.loads(
                command(
                    compose + ["exec", "-T", "api", "python", "-c", VERIFY],
                    environment=environment,
                    phase="persistent state and authentication",
                    input_text=seed,
                )
            )
            deadline = time.monotonic() + 40
            while True:
                health = subprocess.run(
                    compose
                    + ["exec", "-T", "worker", "python", "-m", "app.jobs.worker", "--health"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    timeout=15,
                )
                if health.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Isolated worker consumers did not recover after Redis recreation."
                    )
                time.sleep(0.5)
            evidence.update(
                {"worker_queues": "healthy", "host_ports": "none", "data_containers": "recreated"}
            )
        finally:
            if created:
                assert re.fullmatch(r"omni-recovery-test-[a-f0-9]{12}", project)
                command(
                    compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"],
                    environment=environment,
                    phase="private fixture cleanup",
                )
        print(json.dumps({"status": "passed", **evidence, "cleanup": "private project removed"}))


if __name__ == "__main__":
    main()
