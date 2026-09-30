"""Stop the previous polling bot before deploying the exact trusted main checkout.

Run only through railway-bot.yml. The Railway bot service must have one replica
and NO GitHub source/other automatic deploy trigger. Do not deploy it manually
while this workflow runs: GitHub concurrency cannot lock Railway dashboard users.
An interrupted/failed run can leave the bot stopped; inspect Railway before retrying.
CLI 5.63.1: https://docs.railway.com/cli/{deployment,down,up}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_BOT_SERVICE_ID")
STOPPED = {"REMOVED", "FAILED", "SKIPPED"}
BUILDING = {"QUEUED", "INITIALIZING", "WAITING", "BUILDING", "DEPLOYING"}
KNOWN = STOPPED | BUILDING | {"SUCCESS", "REMOVING", "CRASHED", "SLEEPING", "NEEDS_APPROVAL"}
LIST_LIMIT = 1000


class DeploymentError(Exception):
    """Only fixed operational text or validated IDs/statuses may be exposed."""


def uuid_value(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise DeploymentError("Expected a canonical Railway UUID; check configuration.") from None
    return value


def configured(environment: dict[str, str], *, check_only: bool = False) -> bool:
    missing = [name for name in SCOPES if not environment.get(name)]
    token_present = (
        environment.get("RAILWAY_TOKEN_AVAILABLE") == "true"
        if check_only
        else bool(environment.get("RAILWAY_TOKEN"))
    )
    if not token_present:
        missing.append("RAILWAY_TOKEN")
    if missing:
        message = (
            "Bot deployment skipped: configure repository variables "
            + ", ".join(SCOPES)
            + " and the project-scoped RAILWAY_TOKEN Actions secret. "
            "Missing: " + ", ".join(missing) + ". No Railway change was made."
        )
        print(message)
        if check_only and environment.get("GITHUB_STEP_SUMMARY"):
            with Path(environment["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(message + "\n")
        return False
    for name in SCOPES:
        uuid_value(environment[name])
    return True


def command(args: list[str], *, timeout: int = 45) -> str:
    # Never stream CLI output: errors and build output can contain secret-bearing
    # URLs or runtime data. A failed upload has an unknown outcome; never retry it.
    try:
        result = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError(
            "Command unavailable or timed out. Deployment outcome is unknown; inspect Railway."
        ) from None
    if result.returncode:
        raise DeploymentError(
            "Deployment command failed. The bot may be stopped; inspect Railway before retrying."
        )
    return result.stdout


def require_main_checkout(environment: dict[str, str]) -> str:
    sha = environment.get("GITHUB_SHA", "")
    if (
        environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("GITHUB_REPOSITORY") != "Neytrib/omni-task"
        or environment.get("GITHUB_REF") != "refs/heads/main"
        or environment.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}
        or not re.fullmatch(r"[a-f0-9]{40}", sha)
    ):
        raise DeploymentError("Only trusted main push/manual GitHub Actions runs may deploy.")
    if command(["git", "rev-parse", "HEAD"]).strip() != sha:
        raise DeploymentError("Checkout does not match the requested main commit.")
    if command(["git", "status", "--porcelain", "--untracked-files=all"]).strip():
        raise DeploymentError("Refusing to upload a modified or untracked checkout.")
    if not (ROOT / "deploy/railway/bot.Dockerfile").is_file():
        raise DeploymentError("The bot Dockerfile is missing from this checkout.")
    return sha


def decode(raw: str):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        raise DeploymentError("Railway returned an unexpected response; inspect Railway.") from None


class BotDeployer:
    def __init__(self, environment: dict[str, str]):
        self.scope = [
            "--project",
            environment[SCOPES[0]],
            "--environment",
            environment[SCOPES[1]],
            "--service",
            environment[SCOPES[2]],
        ]

    def railway(self, *args: str, timeout: int = 45) -> str:
        return command(["railway", *args, *self.scope], timeout=timeout)

    def deployments(self) -> dict[str, str]:
        rows = decode(self.railway("deployment", "list", "--json", "--limit", str(LIST_LIMIT)))
        if not isinstance(rows, list) or len(rows) >= LIST_LIMIT:
            raise DeploymentError("Deployment history is invalid or incomplete; inspect Railway.")
        result = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("status"), str)
                or row["status"] not in KNOWN
            ):
                raise DeploymentError("Unknown deployment state; no automatic continuation.")
            key = uuid_value(row.get("id"))
            if key in result:
                raise DeploymentError("Duplicate deployment identifiers; inspect Railway.")
            result[key] = row["status"]
        return result

    def wait_stopped(self, before: dict[str, str], old: str | None) -> None:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.deployments()
            if set(current) - set(before):
                raise DeploymentError(
                    "Concurrent deployment detected. Refusing to start another bot."
                )
            others = {key: state for key, state in current.items() if key != old}
            if any(state not in STOPPED for state in others.values()):
                raise DeploymentError(
                    "Another deployment can still run. Refusing to start the bot."
                )
            if old is None or current.get(old) == "REMOVED":
                return
            if current.get(old) not in {"SUCCESS", "REMOVING"}:
                raise DeploymentError("Previous bot removal was not confirmed. Inspect Railway.")
            time.sleep(5)
        raise DeploymentError("Previous bot did not reach REMOVED within five minutes. No upload.")

    def deploy(self, sha: str) -> str:
        before = self.deployments()
        active = [key for key, state in before.items() if state == "SUCCESS"]
        if len(active) > 1 or any(state not in STOPPED | {"SUCCESS"} for state in before.values()):
            raise DeploymentError(
                "Active or unfinished deployment conflict. Inspect Railway first."
            )
        old = active[0] if active else None
        if old:
            print(
                "Stopping the previous bot; messages remain queued at Telegram during deployment."
            )
            self.railway("down", "--yes")
        self.wait_stopped(before, old)
        # --detach gives the uploaded deployment ID. We verify its own SUCCESS
        # below rather than trusting CLI exit 0 or whichever deployment is newest.
        uploaded = decode(
            self.railway(
                "up", "--ci", "--detach", "--json", "--message", f"main {sha}", timeout=180
            )
        )
        if not isinstance(uploaded, dict):
            raise DeploymentError("Upload outcome is unknown. Inspect Railway before retrying.")
        new_id = uuid_value(uploaded.get("deploymentId"))
        if new_id in before:
            raise DeploymentError("Upload did not identify a new deployment. Inspect Railway.")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current = self.deployments()
            if set(current) - set(before) - {new_id} or any(
                state not in STOPPED for key, state in current.items() if key != new_id
            ):
                raise DeploymentError(
                    "Concurrent deployment detected after upload. Inspect Railway."
                )
            state = current.get(new_id)
            if state == "SUCCESS":
                print(f"Bot deployment {new_id} reached SUCCESS.")
                return new_id
            if state is not None and state not in BUILDING:
                raise DeploymentError(f"Bot deployment reached {state}; no success claimed.")
            time.sleep(5)
        raise DeploymentError(
            "Bot deployment was not confirmed within fifteen minutes. Inspect Railway."
        )


def main() -> int:
    check_only = sys.argv[1:] == ["--check-config"]
    if sys.argv[1:] and not check_only:
        print("Use no arguments to deploy, or --check-config to check setup.", file=sys.stderr)
        return 2
    try:
        ready = configured(os.environ, check_only=check_only)
        if check_only:
            if os.environ.get("GITHUB_OUTPUT"):
                with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                    output.write(f"enabled={'true' if ready else 'false'}\n")
            return 0
        if not ready:
            return 1
        sha = require_main_checkout(os.environ)
        BotDeployer(os.environ).deploy(sha)
        return 0
    except DeploymentError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
