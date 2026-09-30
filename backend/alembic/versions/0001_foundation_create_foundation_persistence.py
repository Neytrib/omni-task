"""Create foundation persistence"""

import sqlalchemy as sa
from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("private_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("task_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "telegram_user_id > 0 AND private_chat_id = telegram_user_id",
            name="ck_users_private_identity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("private_chat_id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_table(
        "login_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_login_links_owner_id"), "login_links", ["owner_id"], unique=False)
    op.create_table(
        "outbox",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "revision", name="uq_outbox_owner_revision"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_secret", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_sessions_owner_id"), "sessions", ["owner_id"], unique=False)
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('telegram_text', 'telegram_voice', 'dashboard')", name="ck_tasks_source"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed')", name="ck_tasks_status"
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 50000", name="ck_tasks_content_length"
        ),
        sa.CheckConstraint("version > 0", name="ck_tasks_version"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tasks_owner_created_id", "tasks", ["owner_id", "created_at", "id"], unique=False
    )
    op.create_table(
        "source_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("bot_identity", sa.String(length=100), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("client_request_id", sa.UUID(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("outcome", sa.String(length=20), server_default="accepted", nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'task_created', 'deleted')", name="ck_receipt_outcome"
        ),
        sa.CheckConstraint(
            "(bot_identity IS NOT NULL AND chat_id IS NOT NULL AND chat_id > 0 "
            "AND message_id IS NOT NULL AND message_id > 0 AND client_request_id IS NULL) OR "
            "(bot_identity IS NULL AND chat_id IS NULL AND message_id IS NULL "
            "AND client_request_id IS NOT NULL)",
            name="ck_receipt_source_identity",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_identity", "chat_id", "message_id", name="uq_receipt_telegram_message"
        ),
        sa.UniqueConstraint("owner_id", "client_request_id", name="uq_receipt_dashboard_request"),
        sa.UniqueConstraint("task_id", name="uq_receipt_task"),
    )
    op.create_index(
        op.f("ix_source_receipts_owner_id"), "source_receipts", ["owner_id"], unique=False
    )
    op.create_table(
        "processing_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("source_receipt_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'processing', 'succeeded', 'failed')", name="ck_processing_state"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_processing_attempts"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_receipt_id"],
            ["source_receipts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_receipt_id"),
    )
    op.create_index(
        op.f("ix_processing_requests_owner_id"), "processing_requests", ["owner_id"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_processing_requests_owner_id"), table_name="processing_requests")
    op.drop_table("processing_requests")
    op.drop_index(op.f("ix_source_receipts_owner_id"), table_name="source_receipts")
    op.drop_table("source_receipts")
    op.drop_index("ix_tasks_owner_created_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(op.f("ix_sessions_owner_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("outbox")
    op.drop_index(op.f("ix_login_links_owner_id"), table_name="login_links")
    op.drop_table("login_links")
    op.drop_table("users")
