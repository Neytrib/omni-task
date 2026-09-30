"""Supervise independent transcription and notification consumers in the worker service."""

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from app.voice_media import cleanup_stale_audio
from omni_logging import configure_logging, log_event


def main() -> None:
    configure_logging("worker")
    hostname = socket.gethostname()
    if "--health" in sys.argv:
        from app.jobs.celery_app import celery_app

        targets = [f"transcription@{hostname}", f"notifications@{hostname}"]
        response = celery_app.control.inspect(destination=targets, timeout=5).ping() or {}
        raise SystemExit(
            0 if all(response.get(name, {}).get("ok") == "pong" for name in targets) else 1
        )
    children = []
    stopping = False
    next_cleanup = 0.0

    def stop(*_):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def sweep_audio():
        nonlocal next_cleanup
        try:
            cleanup_stale_audio(Path(os.environ.get("VOICE_TEMP_DIR", "/tmp/omni-voice")))
        except Exception:
            log_event(
                logging.getLogger("omni_task.voice"),
                "audio_cleanup_delayed",
                level=logging.WARNING,
                error_code="storage",
            )
        next_cleanup = time.monotonic() + 60

    try:
        sweep_audio()
        for name, queues, concurrency in (
            ("transcription", "transcription", 2),
            ("notifications", "notifications,foundation", 1),
        ):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "celery",
                        "-A",
                        "app.jobs.celery_app:celery_app",
                        "--quiet",
                        "worker",
                        "--loglevel=WARNING",
                        f"--concurrency={concurrency}",
                        f"--hostname={name}@{hostname}",
                        f"--queues={queues}",
                    ],
                    env=os.environ.copy(),
                )
            )
        while not stopping and all(child.poll() is None for child in children):
            if time.monotonic() >= next_cleanup:
                sweep_audio()
            time.sleep(0.25)
        if not stopping:
            raise SystemExit(1)
    finally:
        stop()
        deadline = time.monotonic() + 25
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
