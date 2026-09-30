from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "telegram_user_id > 0 AND private_chat_id = telegram_user_id",
            name="ck_users_private_identity",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    private_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    task_revision: Mapped[int] = mapped_column(BigInteger, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed')", name="ck_tasks_status"
        ),
        CheckConstraint(
            "source IN ('telegram_text', 'telegram_voice', 'dashboard')", name="ck_tasks_source"
        ),
        CheckConstraint("char_length(content) BETWEEN 1 AND 50000", name="ck_tasks_content_length"),
        CheckConstraint("version > 0", name="ck_tasks_version"),
        Index("ix_tasks_owner_created_id", "owner_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    source: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceReceipt(Base):
    __tablename__ = "source_receipts"
    __table_args__ = (
        UniqueConstraint(
            "bot_identity", "chat_id", "message_id", name="uq_receipt_telegram_message"
        ),
        UniqueConstraint("owner_id", "client_request_id", name="uq_receipt_dashboard_request"),
        UniqueConstraint("task_id", name="uq_receipt_task"),
        CheckConstraint(
            "(bot_identity IS NOT NULL AND chat_id IS NOT NULL AND chat_id > 0 "
            "AND message_id IS NOT NULL AND message_id > 0 AND client_request_id IS NULL) OR "
            "(bot_identity IS NULL AND chat_id IS NULL AND message_id IS NULL "
            "AND client_request_id IS NOT NULL)",
            name="ck_receipt_source_identity",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'task_created', 'deleted')", name="ck_receipt_outcome"
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bot_identity: Mapped[str | None] = mapped_column(String(100))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    client_request_id: Mapped[UUID | None] = mapped_column(PGUUID)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(30))
    outcome: Mapped[str] = mapped_column(String(20), server_default="accepted")
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessingRequest(Base):
    __tablename__ = "processing_requests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'processing', 'succeeded', 'failed')", name="ck_processing_state"
        ),
        CheckConstraint("attempts >= 0", name="ck_processing_attempts"),
        CheckConstraint("duration_seconds BETWEEN 0 AND 600", name="ck_processing_duration"),
        CheckConstraint(
            "provider_name IN ('disabled', 'fake', 'openai')", name="ck_processing_provider"
        ),
        CheckConstraint(
            "file_size IS NULL OR file_size BETWEEN 1 AND 19000000", name="ck_processing_size"
        ),
        CheckConstraint("ack_message_id IS NULL OR ack_message_id > 0", name="ck_processing_ack"),
        Index("ix_processing_due", "state", "next_attempt_at", "last_dispatched_at"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_receipt_id: Mapped[UUID] = mapped_column(ForeignKey("source_receipts.id"), unique=True)
    file_id: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100))
    ack_message_id: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[int] = mapped_column(Integer, server_default="0")
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    provider_name: Mapped[str] = mapped_column(String(20), server_default="disabled")
    cached_transcript: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'delivering', 'succeeded', 'failed', 'suppressed')",
            name="ck_notification_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_notification_attempts"),
        CheckConstraint(
            "sent_message_id IS NULL OR sent_message_id > 0", name="ck_notification_message"
        ),
        Index("ix_notifications_due", "state", "next_attempt_at", "last_dispatched_at"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    processing_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_requests.id", ondelete="CASCADE"), unique=True
    )
    state: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100))
    sent_message_id: Mapped[int | None] = mapped_column(BigInteger)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("owner_id", "revision", name="uq_outbox_owner_revision"),
        Index(
            "ix_outbox_pending", "created_at", "id", postgresql_where=text("published_at IS NULL")
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(PGUUID)
    revision: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginLink(Base):
    __tablename__ = "login_links"
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrowserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[UUID] = mapped_column(PGUUID, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_secret: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
