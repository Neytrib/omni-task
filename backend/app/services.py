"""Transport-independent task operations. Callers own commit/rollback boundaries."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import Notification, OutboxEvent, ProcessingRequest, SourceReceipt, Task, User


def utcnow() -> datetime:
    return datetime.now(UTC)


def derive_title(content: str) -> str:
    title = " ".join(content.split())
    if len(title) <= 80:
        return title
    prefix = title[:79]
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip() + "…"


def ensure_user(db: Session, telegram_user_id: int, private_chat_id: int) -> User:
    db.execute(
        insert(User)
        .values(id=uuid4(), telegram_user_id=telegram_user_id, private_chat_id=private_chat_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
    )
    return telegram_user(db, telegram_user_id)


def telegram_user(db: Session, telegram_user_id: int) -> User:
    user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        raise AppError(404, "user_not_found", "Start a private conversation with the bot first.")
    return user


def lock_user(db: Session, owner_id: UUID, *, read: bool = False) -> User:
    user = db.scalar(
        select(User)
        .where(User.id == owner_id)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise AppError(404, "user_not_found", "User not found.")
    return user


def owned_task(db: Session, owner_id: UUID, task_id: UUID) -> Task:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == owner_id))
    if task is None:
        raise AppError(404, "task_not_found", "Task not found.")
    return task


def receipt_for(
    db: Session,
    user: User,
    source: str,
    value: str,
    *,
    bot_identity: str | None = None,
    message_id: int | None = None,
    client_request_id: UUID | None = None,
    allow_deleted: bool = False,
) -> tuple[SourceReceipt, bool]:
    fingerprint = hashlib.sha256((source + "\0" + value).encode()).hexdigest()
    if message_id is not None:
        query = select(SourceReceipt).where(
            SourceReceipt.bot_identity == bot_identity,
            SourceReceipt.chat_id == user.private_chat_id,
            SourceReceipt.message_id == message_id,
        )
    else:
        query = select(SourceReceipt).where(
            SourceReceipt.owner_id == user.id,
            SourceReceipt.client_request_id == client_request_id,
        )
    receipt = db.scalar(query)
    if receipt is not None:
        if receipt.owner_id != user.id or receipt.fingerprint != fingerprint:
            raise AppError(
                409, "source_conflict", "This source identity was already used for different input."
            )
        if receipt.outcome == "deleted" and not allow_deleted:
            raise AppError(
                410,
                "source_already_handled",
                "This source was already processed and its task was deleted.",
            )
        return receipt, False
    receipt = SourceReceipt(
        owner_id=user.id,
        bot_identity=bot_identity,
        chat_id=user.private_chat_id if message_id is not None else None,
        message_id=message_id,
        client_request_id=client_request_id,
        fingerprint=fingerprint,
        source=source,
    )
    db.add(receipt)
    db.flush()
    return receipt, True


def record_change(db: Session, user: User, task_id: UUID, event_type: str) -> None:
    user.task_revision += 1
    db.add(
        OutboxEvent(
            owner_id=user.id, task_id=task_id, revision=user.task_revision, event_type=event_type
        )
    )


def create_task(
    db: Session,
    owner_id: UUID,
    content: str,
    source: str,
    *,
    bot_identity: str | None = None,
    message_id: int | None = None,
    client_request_id: UUID | None = None,
) -> tuple[Task, bool]:
    user = lock_user(db, owner_id)
    receipt, created = receipt_for(
        db,
        user,
        source,
        content,
        bot_identity=bot_identity,
        message_id=message_id,
        client_request_id=client_request_id,
    )
    if not created:
        if receipt.task_id is None:
            raise AppError(409, "source_processing", "This source is already being processed.")
        return owned_task(db, user.id, receipt.task_id), False
    task = Task(owner_id=user.id, title=derive_title(content), content=content, source=source)
    db.add(task)
    db.flush()
    receipt.task_id = task.id
    receipt.outcome = "task_created"
    receipt.completed_at = utcnow()
    record_change(db, user, task.id, "task.created")
    db.flush()
    return task, True


def create_processing_request(
    db: Session,
    owner_id: UUID,
    file_id: str,
    bot_identity: str,
    message_id: int,
    *,
    ack_message_id: int | None = None,
    duration_seconds: int,
    file_size: int | None = None,
    provider_name: str = "fake",
) -> tuple[ProcessingRequest, bool]:
    user = lock_user(db, owner_id)
    receipt, created = receipt_for(
        db,
        user,
        "telegram_voice",
        file_id,
        bot_identity=bot_identity,
        message_id=message_id,
        allow_deleted=True,
    )
    if not created:
        result = db.scalar(
            select(ProcessingRequest).where(
                ProcessingRequest.source_receipt_id == receipt.id,
                ProcessingRequest.owner_id == owner_id,
            )
        )
        if result is None:
            raise AppError(409, "source_conflict", "This source identity is already used.")
        return result, False
    result = ProcessingRequest(
        owner_id=user.id,
        source_receipt_id=receipt.id,
        file_id=file_id,
        ack_message_id=ack_message_id,
        duration_seconds=duration_seconds,
        file_size=file_size,
        provider_name=provider_name,
    )
    db.add(result)
    db.flush()
    return result, True


def change_status(db: Session, owner_id: UUID, task_id: UUID, status: str, version: int) -> Task:
    user = lock_user(db, owner_id)
    task = owned_task(db, user.id, task_id)
    if task.version != version:
        raise AppError(409, "stale_version", "The task changed. Reload it and try again.")
    if task.status != status:
        task.status = status
        task.version += 1
        task.updated_at = utcnow()
        record_change(db, user, task.id, "task.updated")
    db.flush()
    return task


def delete_task(db: Session, owner_id: UUID, task_id: UUID, version: int) -> None:
    user = lock_user(db, owner_id)
    task = owned_task(db, user.id, task_id)
    if task.version != version:
        raise AppError(409, "stale_version", "The task changed. Reload it and try again.")
    receipt = db.scalar(
        select(SourceReceipt).where(
            SourceReceipt.task_id == task_id, SourceReceipt.owner_id == owner_id
        )
    )
    if receipt is not None:
        receipt.outcome = "deleted"
        receipt.task_id = None
        processing = db.scalar(
            select(ProcessingRequest)
            .where(
                ProcessingRequest.source_receipt_id == receipt.id,
                ProcessingRequest.owner_id == owner_id,
            )
            .with_for_update()
        )
        if processing is not None:
            processing.cached_transcript = None
            processing.task_id = None
            notification = db.scalar(
                select(Notification)
                .where(
                    Notification.processing_request_id == processing.id,
                    Notification.owner_id == owner_id,
                )
                .with_for_update()
            )
            if notification is not None and notification.state in {"pending", "delivering"}:
                notification.state = "suppressed"
                notification.lease_token = None
                notification.lease_expires_at = None
    record_change(db, user, task.id, "task.deleted")
    db.delete(task)
    db.flush()


def _encode_cursor(payload: dict, signing_key: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(
        signing_key.encode(), b"cursor:" + body.encode(), hashlib.sha256
    ).hexdigest()
    return body + "." + signature


def _decode_cursor(cursor: str, signing_key: str, user: User) -> tuple[datetime, UUID]:
    try:
        body, signature = cursor.split(".")
        expected = hmac.new(
            signing_key.encode(), b"cursor:" + body.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload["owner"] != str(user.id):
            raise ValueError
        if payload["revision"] != user.task_revision:
            raise AppError(
                409, "stale_cursor", "The task list changed. Start again from the first page."
            )
        return datetime.fromisoformat(payload["created"]), UUID(payload["id"])
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise AppError(422, "invalid_cursor", "Invalid pagination cursor.") from exc


def list_tasks(
    db: Session, owner_id: UUID, limit: int, cursor: str | None, signing_key: str
) -> dict:
    # A shared owner-row lock keeps the revision and rows consistent with writers.
    user = lock_user(db, owner_id, read=True)
    query = select(Task).where(Task.owner_id == owner_id)
    if cursor:
        created_at, task_id = _decode_cursor(cursor, signing_key, user)
        query = query.where(tuple_(Task.created_at, Task.id) < tuple_(created_at, task_id))
    rows = list(db.scalars(query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(
            {
                "owner": str(owner_id),
                "revision": user.task_revision,
                "created": last.created_at.isoformat(),
                "id": str(last.id),
            },
            signing_key,
        )
    return {"items": rows, "next_cursor": next_cursor, "revision": user.task_revision}
