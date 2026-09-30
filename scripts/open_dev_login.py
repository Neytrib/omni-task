"""Open a synthetic development login in Arc without printing or saving its bearer link."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSUE_LINK = """
import os
import secrets
import httpx
assert os.environ.get('APP_ENV') == 'development', 'Development only'
assert os.environ.get('DASHBOARD_ORIGIN') in {
    'http://localhost:8080', 'http://127.0.0.1:8080'
}, 'Loopback development origin only'
identity = secrets.randbits(62) + 1
with httpx.Client(base_url='http://127.0.0.1:8000', timeout=10,
    headers={'Authorization': 'Bearer ' + os.environ['BOT_API_KEY']}) as client:
    user = client.post('/internal/bot/users', json={
        'telegram_user_id': identity, 'private_chat_id': identity})
    user.raise_for_status()
    link = client.post('/internal/bot/login-links', json={'telegram_user_id': identity})
    link.raise_for_status()
    print(link.json()['url'])
"""


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This helper uses Arc on macOS. Use the API smoke checks on other hosts.")
    available = subprocess.run(["open", "-Ra", "Arc"], capture_output=True)
    if available.returncode:
        raise SystemExit("Arc is unavailable; no alternative browser was opened.")
    issued = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", ISSUE_LINK],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if issued.returncode:
        raise SystemExit(
            "Could not issue a development link. Check that the development API is ready."
        )
    link = issued.stdout.strip()
    if not link.startswith(
        ("http://localhost:8080/login#token=", "http://127.0.0.1:8080/login#token=")
    ):
        raise SystemExit("Refusing to open a link outside the local development dashboard.")
    opened = subprocess.run(["open", "-a", "Arc", link], capture_output=True)
    if opened.returncode:
        raise SystemExit(
            "Arc could not open the development link; request a fresh one after fixing Arc."
        )
    print("Opened a short-lived synthetic login in Arc. No credentials were printed or saved.")
    print("The synthetic development user remains in PostgreSQL; Log out to revoke this session.")


if __name__ == "__main__":
    main()
