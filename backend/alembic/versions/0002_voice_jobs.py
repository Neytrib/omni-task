"""Durable voice processing and separate notification jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0002_voice_jobs"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "processing_requests", sa.Column("ack_message_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "processing_requests",
        sa.Column("duration_seconds", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("processing_requests", sa.Column("file_size", sa.BigInteger(), nullable=True))
    op.add_column(
        "processing_requests",
        sa.Column("provider_name", sa.String(20), server_default="disabled", nullable=False),
    )
    op.add_column("processing_requests", sa.Column("cached_transcript", sa.Text(), nullable=True))
    op.add_column("processing_requests", sa.Column("task_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_processing_task",
        "processing_requests",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "processing_requests",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "processing_requests",
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("processing_requests", sa.Column("lease_token", sa.UUID(), nullable=True))
    op.add_column(
        "processing_requests",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The foundation accepted metadata-only records without runnable media/ack context.
    # Never start unexpected provider/Telegram work for these historical records.
    op.execute(
        "UPDATE processing_requests SET state = 'failed', error_code = 'legacy_metadata_missing' "
        "WHERE state IN ('queued', 'processing')"
    )
    op.create_check_constraint(
        "ck_processing_duration", "processing_requests", "duration_seconds BETWEEN 0 AND 600"
    )
    op.create_check_constraint(
        "ck_processing_size",
        "processing_requests",
        "file_size IS NULL OR file_size BETWEEN 1 AND 19000000",
    )
    op.create_check_constraint(
        "ck_processing_ack", "processing_requests", "ack_message_id IS NULL OR ack_message_id > 0"
    )
    op.create_check_constraint(
        "ck_processing_provider",
        "processing_requests",
        "provider_name IN ('disabled', 'fake', 'openai')",
    )
    op.create_index(
        "ix_processing_due",
        "processing_requests",
        ["state", "next_attempt_at", "last_dispatched_at"],
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("processing_request_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("sent_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('pending', 'delivering', 'succeeded', 'failed', 'suppressed')",
            name="ck_notification_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_attempts"),
        sa.CheckConstraint(
            "sent_message_id IS NULL OR sent_message_id > 0", name="ck_notification_message"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["processing_request_id"], ["processing_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("processing_request_id"),
    )
    op.create_index("ix_notifications_owner_id", "notifications", ["owner_id"])
    op.create_index(
        "ix_notifications_due", "notifications", ["state", "next_attempt_at", "last_dispatched_at"]
    )


def downgrade():
    op.drop_table("notifications")
    op.drop_index("ix_processing_due", table_name="processing_requests")
    for name in (
        "ck_processing_provider",
        "ck_processing_ack",
        "ck_processing_size",
        "ck_processing_duration",
    ):
        op.drop_constraint(name, "processing_requests", type_="check")
    op.drop_constraint("fk_processing_task", "processing_requests", type_="foreignkey")
    for name in (
        "lease_expires_at",
        "lease_token",
        "last_dispatched_at",
        "next_attempt_at",
        "task_id",
        "cached_transcript",
        "provider_name",
        "file_size",
        "duration_seconds",
        "ack_message_id",
    ):
        op.drop_column("processing_requests", name)
