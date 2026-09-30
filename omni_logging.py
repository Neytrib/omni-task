"""Content-free JSON operational logs shared by HTTP-only bot and backend services."""

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from json import dumps
from uuid import UUID, uuid4

_correlation: ContextVar[str | None] = ContextVar("omni_correlation", default=None)
_service = "application"
_code = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_id_fields = {"request_id", "job_id", "task_id"}
_number_fields = {"attempt", "revision", "http_status", "duration_ms"}
_code_fields = {"error_code", "outcome", "queue"}


def validated_id(value: object) -> str | None:
    if not isinstance(value, (str, UUID)):
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


def current_correlation_id() -> str:
    return _correlation.get() or str(uuid4())


@contextmanager
def correlation_context(value: object = None):
    identifier = validated_id(value) or str(uuid4())
    token = _correlation.set(identifier)
    try:
        yield identifier
    finally:
        _correlation.reset(token)


def _event_data(event: object, correlation_id=None, **fields) -> dict:
    data = {
        "event": event if isinstance(event, str) and _code.fullmatch(event) else "invalid_event",
        "correlation_id": validated_id(correlation_id) or current_correlation_id(),
    }
    for key, value in fields.items():
        if key in _id_fields:
            identifier = validated_id(value)
            if identifier is not None:
                data[key] = identifier
        elif key in _number_fields and type(value) is int and 0 <= value <= 2**63 - 1:
            data[key] = value
        elif key in _code_fields and isinstance(value, str) and _code.fullmatch(value):
            data[key] = value
    return data


def log_event(
    logger: logging.Logger, event: str, *, correlation_id=None, level=logging.INFO, **fields
):
    """Only bounded codes, opaque UUIDs and numbers enter records; no free-form bodies."""
    data = _event_data(event, correlation_id, **fields)
    logger.log(level, data["event"], extra={"omni_event": data})


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": _service,
        }
        if isinstance(getattr(record, "omni_event", None), dict):
            # Apply the allowlist here too, even if code bypasses log_event().
            fields = dict(record.omni_event)
            event = fields.pop("event", None)
            correlation_id = fields.pop("correlation_id", None)
            data.update(_event_data(event, correlation_id, **fields))
        else:
            # Framework exceptions can contain SQL, HTTP credentials or user input.
            # Keep their occurrence and severity, never their message/args/traceback.
            data.update(event="runtime_diagnostic", correlation_id=current_correlation_id())
        return dumps(data, separators=(",", ":"), ensure_ascii=True)


def configure_logging(service: str) -> None:
    global _service
    _service = service if service in {"api", "bot", "worker"} else "application"
    root = logging.getLogger()
    # Preserve test capture handlers, replace only runtime output handlers.
    capture = [h for h in root.handlers if h.__class__.__module__.startswith("_pytest.")]
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJSONFormatter())
    root.handlers = [handler, *capture]
    root.setLevel(logging.WARNING)
    for name in (
        "omni_task",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "celery",
        "celery.task",
    ):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
        logger.setLevel(logging.INFO if name == "omni_task" else logging.WARNING)
