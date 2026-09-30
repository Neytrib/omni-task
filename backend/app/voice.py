"""Durable voice state transitions. Callers commit short transactions around each call.

No provider, broker or Telegram I/O occurs here. Every execution write is fenced by
the current unexpired lease; user-row locks order completion and task deletion.
Check wall time after acquiring locks so lock waits cannot extend an expired lease.
"""

import random
import re
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.models import Notification, ProcessingRequest, SourceReceipt, Task
from app.services import derive_title, lock_user, record_change, utcnow


def _request(db: Session, identifier: UUID):
    owner = db.scalar(select(ProcessingRequest.owner_id).where(ProcessingRequest.id == identifier))
    if owner is None:
        return None
    lock_user(db, owner)
    return db.scalar(
        select(ProcessingRequest)
        .where(ProcessingRequest.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _notification(db: Session, identifier: UUID):
    owner = db.scalar(select(Notification.owner_id).where(Notification.id == identifier))
    if owner is None:
        return None
    lock_user(db, owner)
    return db.scalar(
        select(Notification)
        .where(Notification.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _leased(record, lease: UUID, now: datetime, state: str) -> bool:
    try:
        lease = UUID(str(lease))
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(
        record
        and record.state == state
        and record.lease_token == lease
        and record.lease_expires_at
        and record.lease_expires_at > now
    )


def _clear_lease(record):
    record.lease_token = None
    record.lease_expires_at = None


def _error_code(value: str) -> str:
    return value if re.fullmatch(r"[a-z_]{1,64}", value) else "processing_failed"


def _retry_time(now, attempts, settings, retry_after):
    base = min(settings.voice_retry_base_seconds * 2 ** min(attempts - 1, 8), 300)
    delay = max(base, min(max(retry_after or 0, 0), 300))
    return now + timedelta(seconds=delay + random.uniform(0, min(base * 0.25, 5)))


def _ensure_notification(db, request, now):
    notification = db.scalar(
        select(Notification).where(Notification.processing_request_id == request.id)
    )
    if notification is None:
        notification = Notification(
            owner_id=request.owner_id, processing_request_id=request.id, next_attempt_at=now
        )
        db.add(notification)
        db.flush()
    return notification


def _terminal_failure(db, request, error_code, now):
    request.state = "failed"
    request.error_code = _error_code(error_code)
    request.cached_transcript = None
    request.updated_at = now
    _clear_lease(request)
    _ensure_notification(db, request, now)


def processing_view(db: Session, request: ProcessingRequest) -> dict:
    receipt = db.get(SourceReceipt, request.source_receipt_id)
    notification = db.scalar(
        select(Notification).where(Notification.processing_request_id == request.id)
    )
    return {
        "id": request.id,
        "owner_id": request.owner_id,
        "source_receipt_id": request.source_receipt_id,
        "state": request.state,
        "attempts": request.attempts,
        "error_code": request.error_code,
        "acknowledgement_message_id": request.ack_message_id,
        "duration_seconds": request.duration_seconds,
        "file_size": request.file_size,
        "task_id": request.task_id,
        "deleted": bool(receipt and receipt.outcome == "deleted"),
        "notification_state": notification.state if notification else None,
        "provider_name": request.provider_name,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
    }


def claim_transcription(db: Session, identifier: UUID, settings: Settings, *, now=None):
    request = _request(db, identifier)
    now = now or utcnow()
    if request is None or request.state in {"succeeded", "failed"}:
        return None
    if request.next_attempt_at > now or (
        request.lease_expires_at and request.lease_expires_at > now
    ):
        return None
    if request.cached_transcript is None and request.attempts >= settings.voice_max_attempts:
        _terminal_failure(db, request, "attempts_exhausted", now)
        return None
    request.state = "processing"
    if request.cached_transcript is None:
        request.attempts += 1
    request.lease_token = uuid4()
    request.lease_expires_at = now + timedelta(seconds=settings.voice_lease_seconds)
    request.updated_at = now
    db.flush()
    return {
        "id": str(request.id),
        "owner_id": str(request.owner_id),
        "lease_token": str(request.lease_token),
        "file_id": request.file_id,
        "cached_transcript": request.cached_transcript,
        "attempts": request.attempts,
        "provider_name": request.provider_name,
    }


def renew_transcription(db, identifier, lease, settings, *, now=None) -> bool:
    request = _request(db, identifier)
    now = now or utcnow()
    if not _leased(request, lease, now, "processing"):
        return False
    request.lease_expires_at = now + timedelta(seconds=settings.voice_lease_seconds)
    return True


def save_transcript(db, identifier, lease, transcript, settings, *, now=None) -> bool:
    request = _request(db, identifier)
    now = now or utcnow()
    if not _leased(request, lease, now, "processing"):
        return False
    error = None
    if not isinstance(transcript, str) or not transcript.strip():
        error = "transcript_empty"
    elif len(transcript) > 50000:
        error = "transcript_too_large"
    elif "\x00" in transcript or any(0xD800 <= ord(char) <= 0xDFFF for char in transcript):
        error = "transcript_invalid"
    if error:
        _terminal_failure(db, request, error, now)
        return False
    # A redelivered worker may save only its current fenced result, never rewrite a cache.
    if request.cached_transcript is None:
        request.cached_transcript = transcript
    request.updated_at = now
    return True


def finish_transcription(db, identifier, lease, settings, *, now=None):
    request = _request(db, identifier)
    now = now or utcnow()
    if not _leased(request, lease, now, "processing") or request.cached_transcript is None:
        return None
    receipt = db.get(SourceReceipt, request.source_receipt_id)
    if receipt.outcome == "deleted":
        request.cached_transcript = None
        _clear_lease(request)
        return None
    if receipt.task_id is not None:
        task = db.get(Task, receipt.task_id)
    else:
        content = request.cached_transcript
        task = Task(
            owner_id=request.owner_id,
            title=derive_title(content),
            content=content,
            source="telegram_voice",
        )
        db.add(task)
        db.flush()
        receipt.task_id = task.id
        receipt.outcome = "task_created"
        receipt.completed_at = now
        user = lock_user(db, request.owner_id)
        record_change(db, user, task.id, "task.created")
    request.task_id = task.id
    request.state = "succeeded"
    request.error_code = None
    request.cached_transcript = None
    request.updated_at = now
    _clear_lease(request)
    notification = _ensure_notification(db, request, now)
    db.flush()
    return {"task_id": str(task.id), "notification_id": str(notification.id)}


def fail_transcription(
    db, identifier, lease, error_code, retryable, settings, *, retry_after=None, now=None
):
    request = _request(db, identifier)
    now = now or utcnow()
    if not _leased(request, lease, now, "processing"):
        return False
    if retryable and (
        request.attempts < settings.voice_max_attempts or request.cached_transcript is not None
    ):
        request.state = "queued"
        request.error_code = _error_code(error_code)
        request.next_attempt_at = _retry_time(now, request.attempts, settings, retry_after)
        request.last_dispatched_at = None
        request.updated_at = now
        _clear_lease(request)
    else:
        _terminal_failure(db, request, error_code, now)
    return True


def claim_notification(db, identifier, settings, *, now=None):
    notification = _notification(db, identifier)
    now = now or utcnow()
    if notification is None or notification.state in {"succeeded", "failed", "suppressed"}:
        return None
    if notification.next_attempt_at > now or (
        notification.lease_expires_at and notification.lease_expires_at > now
    ):
        return None
    request = db.get(ProcessingRequest, notification.processing_request_id)
    receipt = db.get(SourceReceipt, request.source_receipt_id)
    if receipt.outcome == "deleted":
        notification.state = "suppressed"
        _clear_lease(notification)
        return None
    if notification.attempts >= settings.notification_max_attempts:
        notification.state = "failed"
        notification.error_code = "attempts_exhausted"
        _clear_lease(notification)
        return None
    notification.state = "delivering"
    notification.attempts += 1
    notification.lease_token = uuid4()
    notification.lease_expires_at = now + timedelta(seconds=settings.voice_lease_seconds)
    notification.updated_at = now
    db.flush()
    return {
        "id": str(notification.id),
        "request_id": str(request.id),
        "lease_token": str(notification.lease_token),
        "attempts": notification.attempts,
    }


def renew_notification(db, identifier, lease, settings, *, now=None):
    notification = _notification(db, identifier)
    now = now or utcnow()
    if not _leased(notification, lease, now, "delivering"):
        return False
    notification.lease_expires_at = now + timedelta(seconds=settings.voice_lease_seconds)
    return True


def finish_notification(db, identifier, lease, settings, *, sent_message_id=None, now=None):
    notification = _notification(db, identifier)
    now = now or utcnow()
    if not _leased(notification, lease, now, "delivering"):
        return False
    if sent_message_id is not None:
        notification.sent_message_id = sent_message_id
    notification.state = "succeeded"
    notification.error_code = None
    notification.updated_at = now
    _clear_lease(notification)
    return True


def fail_notification(
    db, identifier, lease, error_code, retryable, settings, *, retry_after=None, now=None
):
    notification = _notification(db, identifier)
    now = now or utcnow()
    if not _leased(notification, lease, now, "delivering"):
        return False
    notification.error_code = _error_code(error_code)
    notification.updated_at = now
    if retryable and notification.attempts < settings.notification_max_attempts:
        notification.state = "pending"
        notification.next_attempt_at = _retry_time(
            now, notification.attempts, settings, retry_after
        )
        notification.last_dispatched_at = None
    else:
        notification.state = "failed"
    _clear_lease(notification)
    return True


def download_context(db, identifier, lease, settings, *, now=None):
    request = _request(db, identifier)
    now = now or utcnow()
    if not _leased(request, lease, now, "processing"):
        raise AppError(409, "lease_expired", "This processing lease is no longer active.")
    user = lock_user(db, request.owner_id)
    return {
        "id": str(request.id),
        "file_id": request.file_id,
        "telegram_user_id": user.telegram_user_id,
        "acknowledgement_message_id": request.ack_message_id,
        "state": request.state,
        "duration_seconds": request.duration_seconds,
        "file_size": request.file_size,
        "provider_name": request.provider_name,
    }


def notification_context(db, identifier, lease, settings, *, now=None):
    request = _request(db, identifier)
    if request is None:
        raise AppError(404, "processing_not_found", "Processing request not found.")
    notification = db.scalar(
        select(Notification)
        .where(Notification.processing_request_id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    receipt = db.get(SourceReceipt, request.source_receipt_id)
    if notification and receipt.outcome == "deleted":
        return {
            "request_id": str(identifier),
            "state": "deleted",
            "deleted": True,
            "suppressed": True,
            "task": None,
        }
    now = now or utcnow()
    if not _leased(notification, lease, now, "delivering"):
        raise AppError(409, "lease_expired", "This notification lease is no longer active.")
    task = db.get(Task, request.task_id) if request.task_id else None
    user = lock_user(db, request.owner_id)
    return {
        "request_id": str(identifier),
        "notification_id": str(notification.id),
        "telegram_user_id": user.telegram_user_id,
        "state": request.state,
        "deleted": False,
        "suppressed": False,
        "acknowledgement_message_id": request.ack_message_id,
        "sent_message_id": notification.sent_message_id,
        "error_code": request.error_code,
        "provider_name": request.provider_name,
        "task": {
            "id": str(task.id),
            "title": task.title,
            "status": task.status,
            "version": task.version,
            "source": task.source,
        }
        if task
        else None,
    }


def checkpoint_notification(db, identifier, lease, message_id, settings, *, now=None):
    request = _request(db, identifier)
    if request is None:
        return False
    notification = db.scalar(
        select(Notification)
        .where(Notification.processing_request_id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = now or utcnow()
    if not _leased(notification, lease, now, "delivering"):
        return False
    notification.sent_message_id = message_id
    return True


def claim_dispatches(db, settings, limit=20, *, now=None):
    now = now or utcnow()
    retry_before = now - timedelta(seconds=settings.voice_redispatch_seconds)
    result = []
    for model, pending, active, kind in (
        (ProcessingRequest, "queued", "processing", "transcription"),
        (Notification, "pending", "delivering", "notification"),
    ):
        rows = db.scalars(
            select(model)
            .where(
                or_(
                    and_(model.state == pending, model.next_attempt_at <= now),
                    and_(model.state == active, model.lease_expires_at <= now),
                ),
                or_(model.last_dispatched_at.is_(None), model.last_dispatched_at <= retry_before),
            )
            .order_by(model.next_attempt_at, model.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.last_dispatched_at = now
            result.append(
                {
                    "kind": kind,
                    "id": str(row.id),
                    "request_id": str(
                        row.id if kind == "transcription" else row.processing_request_id
                    ),
                }
            )
    return result
