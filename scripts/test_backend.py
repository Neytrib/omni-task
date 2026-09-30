"""Run PostgreSQL-backed tests in an isolated temporary database, then drop only that DB."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main() -> None:
    config = json.loads(
        run("docker", "compose", "config", "--format", "json", capture_output=True).stdout
    )
    database_name = f"omni_task_test_{uuid4().hex}"
    pg = config["services"]["postgres"]["environment"]
    db_user = pg["POSTGRES_USER"]
    environment = os.environ.copy()
    # Credentials travel in environment variables, not command arguments or printed URLs.
    environment["TEST_DATABASE_URL"] = (
        config["services"]["api"]["environment"]["DATABASE_URL"].rsplit("/", 1)[0]
        + "/"
        + database_name
    )
    environment["DATABASE_URL"] = environment["TEST_DATABASE_URL"]
    environment["TEST_REDIS_URL"] = "redis://redis:6379/15"
    environment["REDIS_URL"] = environment["TEST_REDIS_URL"]
    run("docker", "build", "--target", "test", "-t", "omni-task-test:local", ".")
    run("docker", "compose", "exec", "-T", "postgres", "createdb", "-U", db_user, database_name)
    try:
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            config["networks"]["data"]["name"],
            "--env",
            "TEST_DATABASE_URL",
            "--env",
            "DATABASE_URL",
            "--env",
            "TEST_REDIS_URL",
            "--env",
            "REDIS_URL",
            "omni-task-test:local",
            "pytest",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            *(sys.argv[1:] or ["-q"]),
            env=environment,
        )
    finally:
        run(
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "dropdb",
            "-U",
            db_user,
            "--force",
            database_name,
        )


if __name__ == "__main__":
    main()
