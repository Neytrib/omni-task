"""Build and verify the submission in a clean, private Compose project without external APIs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

from package_submission import package

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, environment, phase, timeout=180):
    result = subprocess.run(
        command, cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        # Configuration diagnostics can contain credentials; never echo their body.
        raise RuntimeError(f"Clean setup failed at {phase} (exit {result.returncode})")
    return result.stdout


def main():
    project = "omni-clean-test-" + uuid4().hex[:12]
    environment = os.environ.copy()
    variable_text = (ROOT / ".env.example").read_text() + (ROOT / "docker-compose.yml").read_text()
    names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", variable_text, re.M))
    names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", variable_text))
    names.update({"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"})
    for name in names:
        environment.pop(name, None)
    environment["COMPOSE_DISABLE_ENV_FILE"] = "true"
    (ROOT / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clean-submission-", dir=ROOT / "tmp") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        archive_path = directory / "submission.tar.gz"
        count, _ = package(ROOT, archive_path)
        with tarfile.open(archive_path) as archive:
            for member in archive:
                name = Path(member.name)
                assert member.isfile() and name.parts[0] == "omni-task"
                assert not name.is_absolute() and ".." not in name.parts
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        clean = directory / "omni-task"
        assert not (clean / ".env").exists()
        assert not (clean / "frontend/node_modules").exists()
        assert not (clean / "frontend/dist").exists()
        run(
            ["python3", "scripts/configure_env.py"],
            cwd=clean,
            environment=environment,
            phase="fresh configuration",
        )
        base = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(clean / ".env"),
            "--file",
            str(clean / "docker-compose.yml"),
        ]
        config = json.loads(
            run(
                base + ["config", "--format", "json"],
                cwd=clean,
                environment=environment,
                phase="render isolated configuration",
            )
        )
        for name, service in config["services"].items():
            service.pop("ports", None)
            if name in {"api", "worker", "migrate"}:
                service["image"] = project + "-backend:verification"
            elif name in {"frontend", "bot"}:
                service["image"] = project + "-" + name + ":verification"
        for kind in ("networks", "volumes"):
            assert all(
                item["name"].startswith(project + "_") and not item.get("external")
                for item in config[kind].values()
            )
        assert config["services"]["bot"]["environment"]["TELEGRAM_BOT_TOKEN"] == ""
        worker = config["services"]["worker"]["environment"]
        assert worker["OPENAI_API_KEY"] == "" and worker["ALLOW_PAID_TRANSCRIPTION"] == "false"
        assert worker["TRANSCRIPTION_PROVIDER"] == "disabled"
        configuration = directory / "isolated.json"
        configuration.write_text(json.dumps(config))
        os.chmod(configuration, 0o600)
        compose = ["docker", "compose", "--project-name", project, "--file", str(configuration)]

        def execute(args, phase, timeout=180):
            return run(
                compose + args, cwd=clean, environment=environment, phase=phase, timeout=timeout
            )

        try:
            execute(["build", "api", "bot", "frontend"], "build clean sources", timeout=600)
            execute(
                ["up", "-d", "--no-build", "--wait", "--wait-timeout", "90"],
                "start fresh services",
            )
            rows = [
                json.loads(row)
                for row in execute(["ps", "--format", "json"], "health").splitlines()
            ]
            assert len(rows) == 6 and all(row["Health"] == "healthy" for row in rows)
            for row in rows:
                info = json.loads(
                    run(
                        ["docker", "inspect", row["ID"]],
                        cwd=clean,
                        environment=environment,
                        phase="host port isolation",
                    )
                )[0]
                assert not info["HostConfig"]["PortBindings"]
            probe = json.loads(
                execute(
                    ["exec", "-T", "api", "python", "scripts/queue_smoke.py"],
                    "separate worker probe",
                )
            )
            assert probe["status"] == "passed"
            api_output = execute(
                ["exec", "-T", "api", "python", "scripts/api_smoke.py"], "two-user HTTP walkthrough"
            )
            assert json.loads(api_output.splitlines()[0])["status"] == "passed"
            execute(["exec", "-T", "api", "alembic", "check"], "schema consistency")
            execute(["exec", "-T", "frontend", "nginx", "-t"], "built frontend configuration")
        finally:
            assert re.fullmatch(r"omni-clean-test-[a-f0-9]{12}", project)
            execute(["down", "--volumes", "--remove-orphans", "--timeout", "30"], "private cleanup")
            # Remove only this rehearsal's unique tags, never shared base images or release tags.
            for kind in ("backend", "bot", "frontend"):
                subprocess.run(
                    ["docker", "image", "rm", f"{project}-{kind}:verification"],
                    cwd=clean,
                    env=environment,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "source_files": count,
                    "clean_source_build": "passed",
                    "six_services": "healthy",
                    "two_user_api": "passed",
                    "separate_worker": "passed",
                    "migrations": "consistent",
                    "external_credentials": "absent",
                    "host_ports": "none",
                    "cleanup": "private project removed",
                }
            )
        )


if __name__ == "__main__":
    main()
