"""Create protected, ignored Railway variable files without copying local secrets."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("api", "bot", "worker")
SECRET_PLACEHOLDER = "REPLACE_WITH_THE_SAME_RANDOM_64_CHARACTER_SECRET_ON_ALL_THREE_SERVICES"


def prepare(root: Path) -> list[Path]:
    destination = root / "private" / "railway"
    for directory in (root / "private", destination):
        if directory.is_symlink():
            raise ValueError("Private configuration directories must not be symlinks")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    paths = [destination / f"{service}.env" for service in SERVICES]
    if any(path.is_symlink() for path in paths):
        raise ValueError("Private configuration files must not be symlinks")
    existing = [path.exists() for path in paths]
    if any(existing):
        if not all(existing):
            raise ValueError("Partial configuration exists; preserve it and reconcile manually")
        for path in paths:
            path.chmod(0o600)
        return paths

    shared_key = secrets.token_hex(32)
    contents = []
    for service in SERVICES:
        template = (root / "deploy" / "railway" / f"{service}.env.example").read_text()
        if template.count(SECRET_PLACEHOLDER) != 1:
            raise ValueError(
                "Each service template must contain exactly one shared-key placeholder"
            )
        contents.append(template.replace(SECRET_PLACEHOLDER, shared_key))
    for index, path in enumerate(paths):
        # Exclusive creation prevents a repeated run from overwriting entered values.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(contents[index])
    return paths


def main() -> None:
    for path in prepare(ROOT):
        print(f"Private variable file ready: {path.relative_to(ROOT)} (mode 600)")
    print("Fill the private copies only. Existing values were preserved; no local .env was read.")


if __name__ == "__main__":
    main()
