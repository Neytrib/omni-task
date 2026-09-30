"""Real Redis-backed Celery transport for harmless foundation verification."""

import os
import socket
from uuid import UUID

from celery import Celery
from celery.signals import setup_logging

from omni_logging import configure_logging


@setup_logging.connect
def setup_worker_logging(**_):
    configure_logging("worker")


redis_url = os.environ["REDIS_URL"]
celery_app = Celery(
    "omni_task", broker=redis_url, backend=redis_url, include=["app.jobs.voice_tasks"]
)
celery_app.conf.update(
    task_default_queue="foundation",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_always_eager=False,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": 600,
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "global_keyprefix": os.environ.get("CELERY_BROKER_KEY_PREFIX", ""),
    },
    result_backend_transport_options={
        "global_keyprefix": os.environ.get("CELERY_BROKER_KEY_PREFIX", "")
    },
    result_expires=300,
    task_soft_time_limit=15,
    task_time_limit=20,
)


@celery_app.task(name="foundation.probe")
def probe(nonce: str) -> dict[str, str | int]:
    """Echo a synthetic identifier; never receive content, credentials or provider inputs."""
    UUID(nonce)
    return {"nonce": nonce, "worker_hostname": socket.gethostname(), "worker_pid": os.getpid()}
