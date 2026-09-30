"""Generate ignored local credentials without printing or overwriting existing secrets."""

import os
import secrets
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    template = (root / ".env.example").read_text()
    configured = template.replace(
        "REPLACE_WITH_RANDOM_POSTGRES_PASSWORD", secrets.token_urlsafe(32)
    ).replace("REPLACE_WITH_RANDOM_SERVICE_KEY", secrets.token_urlsafe(48))
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved. Edit it deliberately if configuration changes are needed.")
        return
    with os.fdopen(descriptor, "w") as handle:
        handle.write(configured)
    print("Created private .env with random local credentials (mode 0600).")


if __name__ == "__main__":
    main()
