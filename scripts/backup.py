"""Create a private, atomic PostgreSQL custom-archive bundle through Docker Compose."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc  # noqa: UP017 - host tools also support Python 3.9.
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Use a lowercase PostgreSQL identifier of at most 63 characters.")
    return value


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
        raise ValueError("Secret and backup files must be regular files with mode 0600 or 0400.")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def arguments(parser):
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument(
        "--file",
        "-f",
        action="append",
        type=Path,
        help="Repeat for Compose files; defaults to base and production overlay.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)


class Compose:
    def __init__(self, args):
        private_file(args.env_file)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.project_name):
            raise ValueError("An explicit valid Compose project name is required.")
        if not 1 <= args.timeout_seconds <= 3600:
            raise ValueError("Timeout must be between 1 and 3600 seconds.")
        self.timeout = args.timeout_seconds
        files = args.file or [ROOT / "docker-compose.yml", ROOT / "compose.production.yml"]
        self.environment = os.environ.copy()
        # The protected explicit env file wins over accidentally inherited application settings.
        names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", args.env_file.read_text(), re.M))
        for path in files:
            names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", path.read_text()))
        names.update(
            {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"}
        )
        for name in names:
            self.environment.pop(name, None)
        self.environment["COMPOSE_DISABLE_ENV_FILE"] = "true"
        self.command = [
            "docker",
            "compose",
            "--env-file",
            str(args.env_file.resolve()),
            "--project-name",
            args.project_name,
        ]
        for path in files:
            self.command.extend(["--file", str(path.resolve())])
        configuration = json.loads(self.run(["config", "--format", "json"]))
        if configuration["name"] != args.project_name:
            raise ValueError("Compose project did not match the explicitly requested project.")
        settings = configuration["services"]["postgres"]["environment"]
        self.user = identifier(settings["POSTGRES_USER"])
        self.database = identifier(settings["POSTGRES_DB"])

    def run(self, command, *, stdin=None, stdout=subprocess.PIPE, input_bytes=None):
        result = subprocess.run(
            self.command + command,
            cwd=ROOT,
            env=self.environment,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            input=input_bytes,
            timeout=self.timeout,
        )
        if result.returncode:
            # pg_restore errors may contain task content; Compose output can contain secrets.
            raise RuntimeError("Compose/PostgreSQL operation failed; private data was not logged.")
        return result.stdout

    def pg(self, executable, *options, **kwargs):
        return self.run(
            ["exec", "-T", "postgres", executable, "--username", self.user, *options], **kwargs
        )


def backup(compose, output_dir, database=None):
    database = identifier(database or compose.database)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = output_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Backup output directory must be a private directory with mode 0700.")
    temporary = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=output_dir))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / f"backup-{stamp}-{uuid4().hex[:8]}"
    try:
        archive = temporary / "database.dump"
        with archive.open("xb") as stream:
            archive.chmod(0o600)
            compose.pg("pg_dump", "--format=custom", "--dbname", database, stdout=stream)
            stream.flush()
            os.fsync(stream.fileno())
        if not archive.stat().st_size:
            raise RuntimeError("PostgreSQL produced an empty backup.")
        manifest = {
            "format": "omni-task-postgresql-custom-v1",
            "database": database,
            "created_at": datetime.now(UTC).isoformat(),
            "sha256": digest(archive),
            "bytes": archive.stat().st_size,
        }
        metadata = temporary / "manifest.json"
        with metadata.open("x") as stream:
            metadata.chmod(0o600)
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(temporary)
        temporary.rename(destination)
        sync_directory(output_dir)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database", help="Optional source database; defaults to POSTGRES_DB.")
    args = parser.parse_args()
    try:
        destination = backup(Compose(args), args.output_dir, args.database)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        raise SystemExit(
            "Backup failed. No completed backup was published; private diagnostics suppressed."
        ) from None
    print(json.dumps({"status": "backed_up", "backup": str(destination.resolve())}))


if __name__ == "__main__":
    main()
