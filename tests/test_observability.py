import asyncio
import json
import logging
from uuid import UUID, uuid4

from omni_logging import (
    SafeJSONFormatter,
    correlation_context,
    current_correlation_id,
    log_event,
)


def test_structured_events_allow_only_safe_metadata(caplog):
    identifier = str(uuid4())
    with caplog.at_level(logging.INFO):
        with correlation_context(identifier):
            log_event(
                logging.getLogger("omni_task.test"),
                "transcription_failed",
                job_id=uuid4(),
                attempt=2,
                error_code="provider_timeout",
                queue="transcription",
                transcript="Private transcript must never be logged",
                authorization="Bearer synthetic-secret",
                url="https://secret@example.test/audio",
                file_id="private_file",
                lease_token=str(uuid4()),
            )
    record = caplog.records[-1]
    value = json.loads(SafeJSONFormatter().format(record))
    assert value["correlation_id"] == identifier
    assert value["event"] == "transcription_failed" and value["attempt"] == 2
    assert set(value) == {
        "timestamp",
        "level",
        "service",
        "event",
        "correlation_id",
        "job_id",
        "attempt",
        "error_code",
        "queue",
    }
    UUID(value["job_id"])
    assert "private" not in json.dumps(value).lower()
    assert "synthetic-secret" not in repr(record.__dict__)


def test_framework_exceptions_and_bypassed_extra_cannot_emit_sensitive_payload(caplog):
    sensitive = "https://bot:synthetic-secret@example.test/login#token=private-token"
    with caplog.at_level(logging.WARNING):
        try:
            raise RuntimeError(sensitive)
        except RuntimeError:
            logging.getLogger("celery.worker").exception("Rejected %s", sensitive)
        logging.getLogger("omni_task.test").warning(
            "ignored",
            extra={
                "omni_event": {
                    "event": "notification_failed",
                    "correlation_id": sensitive,
                    "error_code": sensitive,
                    "job_id": sensitive,
                    "duration_ms": sensitive,
                    "transcript": sensitive,
                    "cookie": sensitive,
                }
            },
        )
    for record in caplog.records:
        rendered = SafeJSONFormatter().format(record)
        assert sensitive not in rendered and "private-token" not in rendered
        UUID(json.loads(rendered)["correlation_id"])
    assert (
        json.loads(SafeJSONFormatter().format(caplog.records[0]))["event"] == "runtime_diagnostic"
    )


def test_concurrent_contexts_remain_isolated_and_reset():
    async def exercise():
        first, second = str(uuid4()), str(uuid4())

        async def operation(identifier):
            with correlation_context(identifier):
                await asyncio.sleep(0)
                assert current_correlation_id() == identifier
                assert await asyncio.to_thread(current_correlation_id) == identifier
            assert current_correlation_id() != identifier

        await asyncio.gather(operation(first), operation(second))

    asyncio.run(exercise())


def test_http_logs_preserve_safe_correlation_but_not_credentials(api, caplog):
    identifier = str(uuid4())
    sensitive = "synthetic-private-login-material"
    with caplog.at_level(logging.INFO, logger="omni_task"):
        response = api.get(
            "/api/tasks?token=" + sensitive,
            headers={"X-Correlation-ID": identifier, "Authorization": "Bearer " + sensitive},
        )
    assert response.status_code == 401
    assert response.headers["X-Correlation-ID"] == identifier
    request_id = response.headers["X-Request-ID"]
    UUID(request_id)
    events = [r.omni_event for r in caplog.records if hasattr(r, "omni_event")]
    event = next(e for e in events if e.get("request_id") == request_id)
    assert event["correlation_id"] == identifier and event["http_status"] == 401
    assert sensitive not in json.dumps(events)
    invalid = api.get("/api/session", headers={"X-Correlation-ID": sensitive})
    assert invalid.headers["X-Correlation-ID"] != sensitive
    UUID(invalid.headers["X-Correlation-ID"])
