"""Generate a private production env file; never start services or overwrite secrets."""

import argparse
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configuration(domain: str, email: str, release: str) -> str:
    hostname = re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
        domain,
    )
    if not hostname or not re.fullmatch(r"[^\s@=$#'\"]+@[^\s@=$#'\"]+\.[^\s@=$#'\"]+", email):
        raise ValueError("Use a lowercase public hostname and a plain certificate-contact email")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", release):
        raise ValueError("Release tag must use 1–64 letters, digits, dots, underscores or hyphens")
    content = (ROOT / "production.env.example").read_text()
    for old, new in (
        ("DOMAIN=tasks.example.com", "DOMAIN=" + domain),
        ("ACME_EMAIL=admin@example.com", "ACME_EMAIL=" + email),
        ("RELEASE_TAG=replace-with-release-id", "RELEASE_TAG=" + release),
        ("replace-with-generated-url-safe-secret", secrets.token_urlsafe(32)),
        (
            "replace-with-generated-server-only-secret-at-least-32-characters",
            secrets.token_urlsafe(48),
        ),
    ):
        if old not in content:
            raise ValueError("Production template changed; check configuration generator")
        content = content.replace(old, new)
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        content = configuration(args.domain, args.email, args.release)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print("Created production configuration with random server secrets (mode 0600).")
    print("Polling and transcription remain disabled. Set bot identity/token deliberately.")
    print("No services were started and no public deployment occurred.")


if __name__ == "__main__":
    main()
