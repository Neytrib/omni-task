"""Explicit local smoke: preserve one synthetic task and Redis key across restarts.

Run with host Python from an already healthy development Compose stack. This briefly
restarts PostgreSQL/Redis; it never removes containers, volumes, or unrelated records.
"""

import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

# Credentials stay inside the API container's environment. Only synthetic payloads
# and captured responses pass through stdin/stdout, never the terminal or argv.
HTTP_HELPER = """
import json, os, sys, urllib.error, urllib.request
request = json.load(sys.stdin)
headers = {"Authorization": "Bearer " + os.environ["BOT_API_KEY"]}
headers.update(request.get("headers", {}))
body = request.get("body")
if body is not None:
    body = json.dumps(body).encode()
    headers["Content-Type"] = "application/json"
call = urllib.request.Request(
    "http://127.0.0.1:8000" + request["path"], data=body,
    headers=headers, method=request["method"],
)
try:
    with urllib.request.urlopen(call, timeout=5) as response:
        payload = response.read()
        result = {"status": response.status, "body": json.loads(payload) if payload else {}}
        print(json.dumps(result))
except urllib.error.HTTPError as error:
    print(json.dumps({"status": error.code, "body": {}}))
except Exception:
    print(json.dumps({"status": 503, "body": {}}))
"""


def command(*args, stdin=None, timeout=45):
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("A persistence smoke command exceeded its time limit.") from None
    if result.returncode:
        # Suppress subprocess output: commands may include operational credentials.
        raise RuntimeError("A persistence smoke command failed; no private output was logged.")
    return result.stdout.strip()


def compose(*args, **kwargs):
    return command("docker", "compose", *args, **kwargs)


def api(method, path, body=None, headers=None):
    return json.loads(
        compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            HTTP_HELPER,
            stdin=json.dumps(
                {"method": method, "path": path, "body": body, "headers": headers or {}}
            ),
            timeout=12,
        )
    )


def expect(response, status):
    if response["status"] != status:
        raise RuntimeError(
            f"Synthetic API request expected {status}, received {response['status']}."
        )
    return response["body"]


def redis(*args):
    return compose("exec", "-T", "redis", "redis-cli", "--raw", *args, timeout=12)


def wait_ready(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (RuntimeError, ValueError):
            pass
        time.sleep(1)
    return False


def worker_ready():
    result = compose(
        "exec",
        "-T",
        "worker",
        "celery",
        "-A",
        "app.jobs.celery_app:celery_app",
        "inspect",
        "ping",
        "--timeout",
        "3",
        "--json",
        timeout=10,
    )
    return any(value.get("ok") == "pong" for value in json.loads(result).values())


def bot_ready():
    compose(
        "exec",
        "-T",
        "bot",
        "python",
        "-c",
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8001/health/ready', timeout=5)",
        timeout=10,
    )
    return True


def main():
    config = json.loads(compose("config", "--format", "json"))
    if config["services"]["api"]["environment"].get("APP_ENV") != "development":
        raise RuntimeError("Persistence smoke is enabled only for local development.")
    for service in ("postgres", "redis"):
        if config["services"][service].get("ports"):
            raise RuntimeError("Database and broker must not publish host ports.")
    expect(api("GET", "/api/health/ready"), 200)
    if not worker_ready() or not bot_ready():
        raise RuntimeError("Start with a healthy API, worker, and bot before this smoke.")

    identity = (1 << 62) + secrets.randbits(61)
    query = f"?telegram_user_id={identity}"
    # A fresh synthetic actor cannot modify an existing account's data.
    expect(api("GET", "/internal/bot/tasks" + query), 404)
    expect(
        api(
            "POST",
            "/internal/bot/users",
            {
                "telegram_user_id": identity,
                "private_chat_id": identity,
            },
        ),
        200,
    )
    content = "  Synthetic persistence check\nComplete Unicode content: Հայերեն 🧪  "
    payload = {"telegram_user_id": identity, "message_id": 1, "content": content}
    task = expect(api("POST", "/internal/bot/tasks", payload), 201)
    path = f"/internal/bot/tasks/{task['id']}" + query
    marker_key = "omni-task:persistence-smoke:" + uuid4().hex
    marker_value = uuid4().hex
    cleanup_errors = []
    try:
        if redis("SET", marker_key, marker_value, "EX", "600", "NX") != "OK":
            raise RuntimeError("Could not establish the isolated Redis marker.")
        expect(api("GET", path), 200)
        print(
            "Synthetic task and expiring Redis marker created; restarting PostgreSQL and Redis.",
            flush=True,
        )
        compose("restart", "--no-deps", "--timeout", "20", "postgres", "redis", timeout=50)
        if not wait_ready(lambda: redis("PING") == "PONG"):
            raise RuntimeError("Redis did not recover after the restart.")
        if not wait_ready(lambda: api("GET", "/api/health/ready")["status"] == 200):
            compose("restart", "--no-deps", "--timeout", "10", "api")
            if not wait_ready(lambda: api("GET", "/api/health/ready")["status"] == 200):
                raise RuntimeError("API did not recover after the database/broker restart.")
        for service, probe in (("worker", worker_ready), ("bot", bot_ready)):
            if not wait_ready(probe, timeout=15):
                compose("restart", "--no-deps", "--timeout", "10", service)
                if not wait_ready(probe):
                    raise RuntimeError(f"{service} did not recover after the broker restart.")
        restored = expect(api("GET", path), 200)
        if restored != task or restored["content"] != content:
            raise RuntimeError("PostgreSQL task fields changed across restart.")
        if redis("GET", marker_key) != marker_value:
            raise RuntimeError("Redis marker did not survive restart.")
        replay = expect(api("POST", "/internal/bot/tasks", payload), 200)
        if replay["id"] != task["id"]:
            raise RuntimeError("Source deduplication did not survive restart.")
        print(
            "Task, content, deduplication, and Redis marker survived; services recovered.",
            flush=True,
        )
    finally:
        try:
            restored = expect(api("GET", path), 200)
            if restored["owner_id"] != task["owner_id"] or restored["content"] != content:
                raise RuntimeError("Synthetic cleanup ownership check failed.")
            expect(api("DELETE", path, headers={"If-Match": str(restored["version"])}), 204)
            expect(api("GET", path), 404)
            expect(api("POST", "/internal/bot/tasks", payload), 410)
        except RuntimeError:
            cleanup_errors.append("synthetic task cleanup incomplete")
        try:
            redis("DEL", marker_key)
            if redis("EXISTS", marker_key) != "0":
                raise RuntimeError("Marker cleanup failed.")
        except RuntimeError:
            cleanup_errors.append("Redis marker cleanup incomplete (expires automatically)")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
    print("PASS: task/marker removed; minimal user/source receipt retained; volumes untouched.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Persistence smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
