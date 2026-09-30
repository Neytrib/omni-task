"""Explicitly included only by fault-test workers; never imported by production."""

import json
import os
import time
from pathlib import Path

from app import voice
from celery.signals import task_postrun, task_prerun


def record(phase, task):
    path = os.environ.get("TEST_DELIVERY_EVENTS")
    if path is None:
        return
    evidence = {
        "phase": phase,
        "task_id": task.request.id,
        "task_name": task.name,
        "pid": os.getpid(),
        "redelivered": bool(task.request.delivery_info.get("redelivered")),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(evidence) + "\n").encode())
    finally:
        os.close(descriptor)


@task_prerun.connect(weak=False)
def before_task(sender=None, **_):
    record("start", sender)


@task_postrun.connect(weak=False)
def after_task(sender=None, **_):
    record("finish", sender)


finish_transcription = voice.finish_transcription


def after_cached_commit(*args, **kwargs):
    barrier = os.environ.get("TEST_CACHED_TRANSCRIPT_BARRIER")
    if barrier:
        marker = Path(barrier)
        release = marker.with_suffix(".release")
        if not release.exists():
            marker.write_text(str(os.getpid()))
            deadline = time.monotonic() + 40
            while not release.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Synthetic crash barrier timed out")
                time.sleep(0.02)
    return finish_transcription(*args, **kwargs)


voice.finish_transcription = after_cached_commit
