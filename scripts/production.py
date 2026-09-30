"""Run Compose using protected configuration instead of inherited application variables."""

from __future__ import annotations

import argparse
import subprocess

try:
    from .backup import ROOT, Compose, arguments
except ImportError:
    from backup import ROOT, Compose, arguments


def run(args):
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("Pass a Compose command after --")
    if command[0] == "config" and command != ["config", "--quiet"]:
        raise ValueError("Use config --quiet; rendered configuration exposes credentials")
    compose = Compose(args)
    return subprocess.run(
        compose.command + command,
        cwd=ROOT,
        env=compose.environment,
        timeout=compose.timeout,
    ).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        code = run(args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        # Exceptions from the shared configuration loader omit secret-bearing diagnostics.
        if isinstance(error, subprocess.TimeoutExpired):
            raise SystemExit("Production command exceeded its configured deadline") from None
        raise SystemExit(str(error)) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
