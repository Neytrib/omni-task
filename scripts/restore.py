"""Restore a verified trusted backup into a new database; never overwrite or drop one."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

try:
    from .backup import Compose, arguments, digest, identifier, private_file
except ImportError:
    from backup import Compose, arguments, digest, identifier, private_file


def verify_bundle(directory):
    archive, metadata = directory / "database.dump", directory / "manifest.json"
    private_file(archive)
    private_file(metadata)
    document = json.loads(metadata.read_text())
    if (
        not isinstance(document, dict)
        or document.get("format") != "omni-task-postgresql-custom-v1"
        or type(document.get("bytes")) is not int
        or document["bytes"] <= 0
        or document["bytes"] != archive.stat().st_size
        or document.get("sha256") != digest(archive)
    ):
        raise ValueError("Backup checksum, size, or format did not match its manifest.")
    return archive


def restore(compose, directory, database):
    database = identifier(database)
    if database in {compose.database, "postgres", "template0", "template1"}:
        raise ValueError("Restore target must be a new database, never the configured source.")
    archive = verify_bundle(directory)
    exists = compose.pg(
        "psql",
        "--dbname",
        "postgres",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        "--command",
        f"SELECT 1 FROM pg_database WHERE datname = '{database}'",
    )
    if exists.strip():
        raise ValueError("Restore refuses every existing database, even an empty one.")
    # No --create/--clean: archive database names cannot select or overwrite the destination.
    compose.pg("createdb", "--template=template0", database)
    with archive.open("rb") as stream:
        compose.pg(
            "pg_restore",
            "--dbname",
            database,
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            stdin=stream,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument(
        "--database", required=True, help="New database name; existing names refused."
    )
    args = parser.parse_args()
    try:
        restore(Compose(args), args.backup, args.database)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        raise SystemExit(
            "Restore failed or was refused. Existing databases were not changed. "
            "A newly created empty target may remain; private diagnostics suppressed."
        ) from None
    print(
        json.dumps({"status": "restored", "database": args.database, "application": "not_started"})
    )


if __name__ == "__main__":
    main()
