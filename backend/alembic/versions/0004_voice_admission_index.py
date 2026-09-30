"""Index the durable rolling voice-admission window."""

from alembic import op

revision = "0004_voice_admission_index"
down_revision = "0003_live_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_processing_created", "processing_requests", ["created_at"])


def downgrade():
    op.drop_index("ix_processing_created", table_name="processing_requests")
