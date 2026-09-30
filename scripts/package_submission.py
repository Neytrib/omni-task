"""Create a source-only submission; never copy the workspace wholesale."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".env.example",
    ".gitignore",
    ".dockerignore",
    "AGENTS.md",
    "DESIGN.md",
    "SPEC.md",
    "TASKS.md",
    "README.md",
    "Dockerfile",
    "docker-compose.yml",
    "compose.production.yml",
    "production.env.example",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "omni_logging.py",
}
SOURCE_DIRS = {"backend", "bot", "frontend", "scripts", "tests", "docs", "deploy"}
EXACT_SOURCES = {
    ".railway/railway.ts",
    ".railway/package.json",
    ".railway/package-lock.json",
    ".railway/tsconfig.json",
    ".railway/config.test.mjs",
    ".railway/README.md",
    "deploy/railway/backend.Dockerfile",
    "deploy/railway/bot.Dockerfile",
    "deploy/railway/api.env.example",
    "deploy/railway/bot.env.example",
    "deploy/railway/worker.env.example",
}
EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "tmp",
    "data",
    "uploads",
    "secrets",
    "private",
    "confidential",
    "recordings",
    "backups",
    "source-documents",
    "vacancy-private",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
    ".vitest",
    "assessment-private",
}
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".css", ".html", ".json", ".md", ".txt", ".conf", ".mako"}
ENV_TEMPLATES = {
    ".env.example",
    "production.env.example",
    "deploy/railway/api.env.example",
    "deploy/railway/bot.env.example",
    "deploy/railway/worker.env.example",
}
# Apply this before the source allowlist, including in extracted trees without Git.
# Check every suffix so adding .json/.txt to a private filename cannot publish it.
PRIVATE_SUFFIXES = frozenset(
    ".env .pem .key .p12 .pfx .jks .keystore .pdf .doc .docx .odt .rtf "
    ".ogg .opus .wav .mp3 .m4a .webm .oga .flac .aac .mp4 .mov "
    ".log .dump .backup .sql .db .sqlite .sqlite3 .rdb .aof "
    ".zip .tar .tgz .gz .7z .bak .orig .swp .swo".split()
)
SECRET_NAMES = {
    "POSTGRES_PASSWORD",
    "BOT_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "RAILWAY_TOKEN",
    "RAILWAY_API_TOKEN",
}
SUSPICIOUS = re.compile(
    rb"(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|\b[0-9]{7,}:[A-Za-z0-9_-]{30,}|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def local_secrets(root: Path) -> set[bytes]:
    """Read only known local secret values for comparison; never emit them."""
    values = set()
    locations = [
        root / ".env",
        *(root / "private/railway" / f"{name}.env" for name in ("api", "bot", "worker", "deploy")),
    ]
    for env in locations:
        if not env.is_file() or any(part.is_symlink() for part in (env, *env.parents)):
            continue
        for line in env.read_text().splitlines():
            key, separator, value = line.partition("=")
            value = value.strip().strip("\"'")
            if separator and key.strip() in SECRET_NAMES and len(value) >= 16:
                if "REPLACE_" not in value and not value.startswith("${{"):
                    values.add(value.encode())
    return values


def private_source(member: Path) -> bool:
    if member.as_posix() in ENV_TEMPLATES:
        return False
    for part in member.parts:
        name = part.casefold()
        suffixes = set(Path(name).suffixes)
        if (
            name in EXCLUDED
            or name.startswith(".env")
            or bool(suffixes & PRIVATE_SUFFIXES)
            or name.endswith("~")
            or "ai_engineering_intern_technical_task" in name
            or (
                ".json" in suffixes
                and (name.startswith("credentials.") or name.startswith("service-account"))
            )
        ):
            return True
    return False


def collect_sources(root: Path) -> dict[str, bytes]:
    result = {}
    secrets = local_secrets(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        dirs[:] = sorted(
            name
            for name in dirs
            if not private_source(relative / name)
            and (
                not name.startswith(".") or (not relative.parts and name in {".railway", ".github"})
            )
            and not (Path(directory) / name).is_symlink()
            and (relative.parts or name in SOURCE_DIRS | {".railway", ".github"})
            and not (relative.parts == (".railway",))
            and not (relative.parts == (".github",) and name != "workflows")
        )
        for name in sorted(files):
            path = Path(directory) / name
            member = path.relative_to(root)
            if private_source(member):
                continue
            if member.as_posix() in EXACT_SOURCES:
                allowed = True
            elif member.parent.parts == (".github", "workflows"):
                allowed = path.suffix in {".yml", ".yaml"} and not name.startswith(".")
            elif not member.parent.parts:
                allowed = name in ROOT_FILES
            else:
                allowed = (
                    not name.startswith(".")
                    and member.parts[0] in SOURCE_DIRS
                    and (path.suffix in SOURCE_SUFFIXES or name in {"Dockerfile", "Caddyfile"})
                    and "AI_Engineering_Intern_Technical_Task" not in name
                )
            if not allowed:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Submission source is not a regular file: {member}")
            content = path.read_bytes()
            if SUSPICIOUS.search(content) or any(value in content for value in secrets):
                raise ValueError(f"Potential credential in submission source: {member}")
            result[member.as_posix()] = content
    required = {"README.md", "docker-compose.yml", "Dockerfile", "pyproject.toml", "uv.lock"}
    if not required.issubset(result):
        raise ValueError("Incomplete source tree; required project files are missing")
    return dict(sorted(result.items()))


def package(root: Path, output: Path) -> tuple[int, str]:
    sources = collect_sources(root)
    manifest = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sources.items()
    ).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            os.fdopen(descriptor, "wb") as handle,
            tarfile.open(fileobj=handle, mode="w:gz") as archive,
        ):
            for name, content in {**sources, "MANIFEST.sha256": manifest}.items():
                member = tarfile.TarInfo("omni-task/" + name)
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return len(sources), digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/omni-task-submission.tar.gz")
    args = parser.parse_args()
    try:
        count, digest = package(ROOT, args.output)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print(f"Created source-only submission: {args.output} ({count} files + manifest)")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    main()
