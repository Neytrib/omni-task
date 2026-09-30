"""Index committed live hints awaiting publication."""

import sqlalchemy as sa
from alembic import op

revision = "0003_live_outbox"
down_revision = "0002_voice_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_outbox_pending",
        "outbox",
        ["created_at", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_outbox_pending", table_name="outbox")
