"""Check prospective Git inclusion without initializing the application tree."""

import os
import subprocess
from pathlib import Path

from scripts.package_submission import collect_sources

ROOT = Path(__file__).resolve().parents[1]


def test_git_ignores_private_files_but_keeps_complete_source(tmp_path):
    # This throwaway repository contains only paths and synthetic empty files.
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, env=environment)
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    private = [
        ".env",
        ".env.local",
        ".env.production",
        ".envrc",
        "production.env",
        "frontend/.env",
        "frontend/.env.example",
        "backend/secrets.env.old",
        "assessment.pdf",
        "Vacancy.PDF",
        "documents/source.Pdf",
        "documents/assessment.docx",
        "source.DOCX",
        "assessment-private/extract.txt",
        "vacancy-private/source.md",
        "source-documents/source.txt",
        "confidential/source.md",
        "secrets/settings.json",
        "private/settings.yaml",
        "private/railway/api.env",
        "private/railway/bot.env",
        "private/railway/worker.env",
        "deploy/railway/api.env",
        "deploy/railway/secret.env.example",
        "credentials.json",
        "service-account-prod.json",
        "backend/credentials.json",
        "frontend/credentials.production.json",
        "frontend/service-account-production.json",
        "deploy/prod.env.json",
        "backend/settings.env.py",
        "docs/worker.log.json",
        "docs/database.sql.txt",
        ".netrc",
        ".pgpass",
        ".npmrc",
        "signing.pem",
        "client.P12",
        "recording.ogg",
        "Recording.WAV",
        "audio.m4a",
        "video.MP4",
        "recordings/anything.txt",
        "worker.log",
        "worker.log.1",
        "runtime.LOG",
        "database.dump",
        "database.DUMP",
        "database.sql",
        "database.sql.gz",
        "database.sqlite3",
        "dump.RDB",
        "appendonly.aof",
        "backup.tar.gz",
        "backup.ZIP",
        "config.json.bak",
        "data/state.json",
        "uploads/content.txt",
        "tmp/report.txt",
        "dist/submission.tar.gz",
        "frontend/dist/index.html",
        "frontend/node_modules/key.js",
        ".venv/config.py",
        "backend/__pycache__/settings.pyc",
    ]
    public = [
        ".env.example",
        "production.env.example",
        ".gitignore",
        ".dockerignore",
        "Dockerfile",
        "docker-compose.yml",
        "compose.production.yml",
        "README.md",
        "backend/app/main.py",
        "bot/app/main.py",
        "backend/app/jobs/worker.py",
        "frontend/src/App.tsx",
        "frontend/package-lock.json",
        "uv.lock",
        "pyproject.toml",
        ".github/workflows/pages.yml",
        "deploy/railway/api.env.example",
        "deploy/railway/bot.env.example",
        "deploy/railway/worker.env.example",
        "deploy/railway/backend.Dockerfile",
        "deploy/railway/bot.Dockerfile",
        ".railway/railway.ts",
        "docs/HOSTING_PLAN.md",
    ]
    for relative in private + public:
        path = tmp_path / relative
        if relative == ".gitignore":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.excludesFile={os.devnull}",
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ],
        cwd=tmp_path,
        env=environment,
        input="\0".join(private + public).encode() + b"\0",
        capture_output=True,
        check=True,
    )
    ignored = set(result.stdout.decode().strip("\0").split("\0"))
    assert set(private) <= ignored
    assert not set(public) & ignored
    sources = collect_sources(tmp_path)
    assert not set(private) & sources.keys()
    assert set(public) <= sources.keys()
