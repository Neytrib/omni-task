import hashlib
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from scripts.backup import Compose
from scripts.configure_production import configuration
from scripts.package_submission import collect_sources, package
from scripts.production import run as run_production


def source_tree(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("README.md", "docker-compose.yml", "Dockerfile", "pyproject.toml", "uv.lock"):
        (root / name).write_text("Synthetic source\n")
    (root / "frontend/src").mkdir(parents=True)
    (root / "frontend/src/main.tsx").write_text("export const title = 'Task board';\n")
    return root


def test_submission_omits_runtime_data_hidden_files_recordings_and_external_symlinks(tmp_path):
    root = source_tree(tmp_path / "source")
    private = b"synthetic private material"
    for relative in (
        ".env",
        "data/private.dump",
        "tmp/transcript.txt",
        "frontend/node_modules/key.js",
        "frontend/dist/runtime.js",
        "frontend/.env.production",
        "frontend/private.wav",
        "backend/confidential.pdf",
        "backend/__pycache__/record.py",
        "frontend/recording.webm",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(private)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "record.py").write_bytes(private)
    (root / "backend/private-directory").symlink_to(outside, target_is_directory=True)
    output = tmp_path / "submission.tar.gz"
    count, digest = package(root, output)
    assert count == 6 and hashlib.sha256(output.read_bytes()).hexdigest() == digest
    with tarfile.open(output) as archive:
        assert all(member.isfile() for member in archive.getmembers())
        assert not any(private in archive.extractfile(member).read() for member in archive)
        manifest = archive.extractfile("omni-task/MANIFEST.sha256").read().decode()
        for row in manifest.splitlines():
            expected, name = row.split("  ")
            assert (
                hashlib.sha256(archive.extractfile("omni-task/" + name).read()).hexdigest()
                == expected
            )
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        package(root, output)


def test_submission_rejects_known_credentials_and_source_symlinks(tmp_path):
    root = source_tree(tmp_path)
    secret = "synthetic-secret-value-never-to-submit"
    (root / ".env").write_text("BOT_API_KEY=" + secret)
    leaked = root / "frontend/src/leak.ts"
    leaked.write_text(secret)
    with pytest.raises(ValueError, match="Potential credential"):
        collect_sources(root)
    leaked.unlink()
    leaked.symlink_to(root / "README.md")
    with pytest.raises(ValueError, match="not a regular file"):
        collect_sources(root)


def test_submission_detects_key_patterns_without_reading_private_env(tmp_path):
    root = source_tree(tmp_path)
    (root / "frontend/src/leak.ts").write_text("sk-" + "a" * 35)
    with pytest.raises(ValueError, match="Potential credential"):
        collect_sources(root)


def test_submission_includes_only_explicit_hosted_configs_and_workflows(tmp_path):
    root = source_tree(tmp_path)
    public = {
        ".railway/railway.ts",
        ".railway/package.json",
        ".railway/package-lock.json",
        ".railway/tsconfig.json",
        ".railway/config.test.mjs",
        ".railway/README.md",
        ".github/workflows/pages.yml",
        "deploy/railway/backend.Dockerfile",
        "deploy/railway/bot.Dockerfile",
        "deploy/railway/api.env.example",
        "deploy/railway/bot.env.example",
        "deploy/railway/worker.env.example",
    }
    private = {
        ".railway/session.json",
        ".railway/node_modules/railway/index.js",
        ".github/credentials.json",
        ".github/workflows/settings.env",
        "private/railway/api.env",
        "deploy/railway/api.env",
        "deploy/railway/unknown.env.example",
        "backend/private/credentials.json",
        "backend/confidential/source.txt",
    }
    for name in public | private:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic config\n")
    sources = collect_sources(root)
    assert public <= sources.keys()
    assert not private & sources.keys()


@pytest.mark.parametrize(
    "file_name,variable",
    [("api.env", "REDIS_URL"), ("deploy.env", "RAILWAY_TOKEN")],
)
def test_submission_detects_credentials_from_private_hosted_configuration(
    tmp_path, file_name, variable
):
    root = source_tree(tmp_path)
    credentials = root / "private/railway" / file_name
    credentials.parent.mkdir(parents=True)
    secret = "rediss://default:synthetic-private-value@redis.example.invalid:6379/0"
    credentials.write_text(variable + "=" + secret)
    (root / "frontend/src/leak.ts").write_text(secret)
    with pytest.raises(ValueError, match="Potential credential"):
        collect_sources(root)


def test_production_configuration_generates_distinct_secrets_without_enabling_external_calls():
    first = configuration("tasks.example.com", "admin@example.com", "release-1")
    second = configuration("tasks.example.com", "admin@example.com", "release-1")

    def values(content):
        return dict(
            row.split("=", 1) for row in content.splitlines() if row and not row.startswith("#")
        )

    initial, repeated = values(first), values(second)
    assert initial["BOT_API_KEY"] != repeated["BOT_API_KEY"]
    assert initial["POSTGRES_PASSWORD"] != repeated["POSTGRES_PASSWORD"]
    assert len(initial["BOT_API_KEY"]) >= 32
    assert initial["TELEGRAM_BOT_TOKEN"] == initial["OPENAI_API_KEY"] == ""
    assert initial["TRANSCRIPTION_PROVIDER"] == "disabled"
    assert initial["ALLOW_PAID_TRANSCRIPTION"] == "false"


@pytest.mark.parametrize(
    "domain,email,release",
    [
        ("https://tasks.example.com", "admin@example.com", "r1"),
        ("tasks.example.com\nOPENAI_API_KEY=bad", "admin@example.com", "r1"),
        ("tasks.example.com", "admin@example.com\nALLOW_PAID_TRANSCRIPTION=true", "r1"),
        ("tasks.example.com", "admin@example.com", "r1\nOTHER=true"),
    ],
)
def test_production_configuration_rejects_env_injection(domain, email, release):
    with pytest.raises(ValueError):
        configuration(domain, email, release)


def test_protected_compose_file_wins_over_inherited_environment(tmp_path, monkeypatch):
    env = tmp_path / "production.env"
    env.write_text("RELEASE_TAG=expected\nPOSTGRES_DB=expected_database\n")
    env.chmod(0o600)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("image: backend:${RELEASE_TAG}\ncredential: ${BOT_API_KEY}\n")
    for name in ("RELEASE_TAG", "POSTGRES_DB", "BOT_API_KEY", "COMPOSE_FILE"):
        monkeypatch.setenv(name, "inherited-wrong-value")

    def execute(command, **options):
        assert command[-3:] == ["config", "--format", "json"]
        assert not {"RELEASE_TAG", "POSTGRES_DB", "BOT_API_KEY", "COMPOSE_FILE"} & set(
            options["env"]
        )
        assert options["env"]["COMPOSE_DISABLE_ENV_FILE"] == "true"
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "name": "omni-task-prod",
                    "services": {
                        "postgres": {
                            "environment": {
                                "POSTGRES_USER": "omni_task",
                                "POSTGRES_DB": "expected_database",
                            }
                        }
                    },
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(subprocess, "run", execute)
    compose = Compose(
        SimpleNamespace(
            env_file=env,
            project_name="omni-task-prod",
            file=[compose_file],
            timeout_seconds=60,
        )
    )
    assert compose.database == "expected_database"


@pytest.mark.parametrize("command", [["config"], ["config", "--format", "json"]])
def test_production_wrapper_rejects_secret_bearing_configuration_output(command):
    with pytest.raises(ValueError, match="exposes credentials"):
        run_production(SimpleNamespace(command=command))
