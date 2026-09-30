"""Run inside the API container to prove a separate worker consumes a Redis job."""

import json
import socket
from uuid import uuid4

from app.jobs.celery_app import celery_app

nonce = str(uuid4())
result = celery_app.send_task("foundation.probe", args=[nonce], queue="foundation")
try:
    reply = result.get(timeout=30)
    assert reply["nonce"] == nonce, "Unexpected probe response"
    assert reply["worker_hostname"] != socket.gethostname(), "Job ran in the sender container"
    assert not celery_app.conf.task_always_eager, "Probe must traverse Redis"
    print(json.dumps({"status": "passed", "transport": "redis", **reply}))
finally:
    result.forget()
