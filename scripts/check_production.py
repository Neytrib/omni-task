"""Exercise the production HTTPS topology privately with synthetic credentials.

Requires the three built :local images and the pinned infrastructure images. All
test containers use a unique project, no host ports, and a private test CA. This
never starts public ACME, modifies host trust, or uses real Telegram/OpenAI keys.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def command(args, *, environment, phase, include_stderr=False):
    result = subprocess.run(
        args, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=150
    )
    if result.returncode:
        # Compose config, client responses, and runtime logs can contain secrets.
        # The fixture client emits only an exception type/line marker on failure.
        marker = re.search(r"HTTPS_FIXTURE_FAILED [A-Za-z]+ line=[0-9]+", result.stderr)
        detail = ": " + marker[0] if marker else ""
        raise RuntimeError(f"Production check failed at {phase} (exit {result.returncode}){detail}")
    return result.stdout + result.stderr if include_stderr else result.stdout


def verify_production_configuration(configuration, project, release, domain):
    assert configuration["name"] == project
    services = configuration["services"]
    for name, service in services.items():
        ports = service.get("ports", [])
        if name == "edge":
            assert {(int(port["published"]), port["target"]) for port in ports} == {
                (80, 80),
                (443, 443),
            }
            assert all(port["protocol"] == "tcp" for port in ports)
        else:
            assert not ports
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }
    for name in ("api", "migrate", "worker"):
        service = services[name]
        assert service["image"] == "omni-task-backend:" + release
        assert service["environment"]["APP_ENV"] == "production"
        assert service["environment"]["COOKIE_SECURE"] == "true"
        assert service["environment"]["DASHBOARD_ORIGIN"] == "https://" + domain
    for name in ("bot", "frontend"):
        assert services[name]["image"] == "omni-task-" + name + ":" + release
    assert "DATABASE_URL" not in services["bot"]["environment"]
    assert "REDIS_URL" not in services["bot"]["environment"]
    assert set(services["bot"]["networks"]) == {"service"}
    assert set(services["edge"]["networks"]) == {"edge"}
    assert set(services["frontend"]["networks"]) == {"service", "edge"}
    assert configuration["networks"]["data"]["internal"]
    for resource in ("volumes", "networks"):
        for item in configuration[resource].values():
            assert item["name"].startswith(project + "_") and not item.get("external")


def main():
    project = "omni-production-test-" + uuid4().hex[:12]
    release = project
    domain = "omni-production.test"
    environment = os.environ.copy()
    source = "\n".join(
        (ROOT / name).read_text()
        for name in (
            "docker-compose.yml",
            "compose.production.yml",
            ".env.example",
            "production.env.example",
        )
    )
    names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", source))
    names.update(re.findall(r"^([A-Z][A-Z0-9_]*)=", source, re.M))
    names.update({"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"})
    for name in names:
        environment.pop(name, None)
    environment["COMPOSE_DISABLE_ENV_FILE"] = "true"
    private_values = [secrets.token_urlsafe(32), secrets.token_urlsafe(48)]
    fixture_environment = {
        "POSTGRES_USER": "omni_production_test",
        "POSTGRES_DB": "omni_production_test",
        "POSTGRES_PASSWORD": private_values[0],
        "BOT_API_KEY": private_values[1],
        "BOT_IDENTITY": project,
        "DOMAIN": domain,
        "ACME_EMAIL": "nobody@example.test",
        "RELEASE_TAG": release,
        "LIVE_CHANNEL": project + ":live",
        "TRANSCRIPTION_PROVIDER": "fake",
        "TELEGRAM_BOT_TOKEN": "",
        "OPENAI_API_KEY": "",
        "ALLOW_PAID_TRANSCRIPTION": "false",
    }
    temporary_root = ROOT / "tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="production-check-", dir=temporary_root) as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        envfile = directory / "fixture.env"
        envfile.write_text(
            "".join(f"{key}={value}\n" for key, value in fixture_environment.items())
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
            "--file",
            str(ROOT / "compose.production.yml"),
        ]
        configuration = json.loads(
            command(
                initial + ["config", "--format", "json"],
                environment=environment,
                phase="production configuration",
            )
        )
        verify_production_configuration(configuration, project, release, domain)
        for service in configuration["services"].values():
            service.pop("ports", None)
            service.pop("build", None)
        assert configuration["services"]["bot"]["environment"]["TELEGRAM_BOT_TOKEN"] == ""
        worker_environment = configuration["services"]["worker"]["environment"]
        assert worker_environment["OPENAI_API_KEY"] == ""
        assert worker_environment["ALLOW_PAID_TRANSCRIPTION"] == "false"
        assert worker_environment["TRANSCRIPTION_PROVIDER"] == "fake"
        configuration["services"]["api"]["environment"]["LIVE_HEARTBEAT_SECONDS"] = "1"
        caddyfile = directory / "Caddyfile"
        caddy_source = (ROOT / "deploy/Caddyfile").read_text()
        assert caddy_source.count("{$DOMAIN} {") == 1
        caddyfile.write_text(caddy_source.replace("{$DOMAIN} {", "{$DOMAIN} {\n\ttls internal", 1))
        os.chmod(caddyfile, 0o644)
        edge = configuration["services"]["edge"]
        edge["networks"]["edge"] = {"aliases": [domain]}
        for mount in edge["volumes"]:
            if mount["target"] == "/etc/caddy/Caddyfile":
                mount["source"] = str(caddyfile)
        certificate = directory / "root.crt"
        certificate.touch(mode=0o644)
        configuration["services"]["verifier"] = {
            "image": "omni-task-backend:" + release,
            "profiles": ["verification"],
            "command": ["python", "/checks/check_https.py"],
            "networks": {"edge": {}, "service": {}},
            "environment": {"DOMAIN": domain, "BOT_API_KEY": private_values[1]},
            "volumes": [
                {
                    "type": "bind",
                    "source": str(certificate),
                    "target": "/trust/root.crt",
                    "read_only": True,
                },
                {
                    "type": "bind",
                    "source": str(ROOT / "deploy/check_https.py"),
                    "target": "/checks/check_https.py",
                    "read_only": True,
                },
            ],
        }
        fixture = directory / "compose.json"
        fixture.write_text(json.dumps(configuration))
        os.chmod(fixture, 0o600)
        compose = ["docker", "compose", "--project-name", project, "--file", str(fixture)]
        tagged = []
        created = False
        evidence = {}
        try:
            for kind in ("backend", "bot", "frontend"):
                target = "omni-task-" + kind + ":" + release
                command(
                    ["docker", "tag", "omni-task-" + kind + ":local", target],
                    environment=environment,
                    phase="temporary release image tag",
                )
                tagged.append(target)
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
                phase="private production startup",
            )
            for service in ("api", "bot", "worker", "frontend", "postgres", "redis", "edge"):
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
                        phase="runtime boundary",
                    )
                )[0]
                assert not inspected["HostConfig"]["PortBindings"]
                assert inspected["State"]["Health"]["Status"] == "healthy"
            command(
                compose
                + ["cp", "edge:/data/caddy/pki/authorities/local/root.crt", str(certificate)],
                environment=environment,
                phase="private CA public certificate",
            )
            os.chmod(certificate, 0o644)
            evidence = json.loads(
                command(
                    compose + ["run", "--rm", "--no-deps", "-T", "verifier"],
                    environment=environment,
                    phase="HTTPS and WSS behavior",
                )
            )
            private_values.extend(evidence.pop("private_secrets"))
            command(
                compose + ["stop", "frontend"],
                environment=environment,
                phase="fixture upstream outage",
            )
            sentinel = "private-log-probe-" + secrets.token_hex(24)
            private_values.append(sentinel)
            command(
                compose
                + [
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "-e",
                    "FAILURE_PROBE=true",
                    "-e",
                    "LOG_SENTINEL=" + sentinel,
                    "verifier",
                ],
                environment=environment,
                phase="failed upstream privacy probe",
            )
            # Docker Compose writes container log streams to stdout here; keep them private.
            logs = command(
                compose + ["logs", "--no-color", "--no-log-prefix"],
                environment=environment,
                phase="private operational log check",
                include_stderr=True,
            )
            assert all(value not in logs for value in private_values)
            assert '"status":502' in logs or '"status": 502' in logs
            evidence.update(
                {
                    "production_ports": "only_80_443",
                    "fixture_ports": "none",
                    "services": "seven_healthy",
                    "log_secrets_and_failure_url": "absent",
                    "tls_trust": "private_test_CA_only_no_host_changes",
                }
            )
        finally:
            if created:
                command(
                    compose + ["down", "--volumes", "--remove-orphans"],
                    environment=environment,
                    phase="fixture project cleanup",
                )
            for target in tagged:
                command(
                    ["docker", "image", "rm", target],
                    environment=environment,
                    phase="temporary image tag cleanup",
                )
        for resource in ("container", "network", "volume"):
            remaining = command(
                [
                    "docker",
                    resource,
                    "ls",
                    "--quiet",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                ],
                environment=environment,
                phase="cleanup inventory",
            )
            assert not remaining.strip()
        print(json.dumps({"status": "passed", "cleanup": "private_project_removed", **evidence}))


if __name__ == "__main__":
    main()
