"""Index bounded log retention scans."""

from alembic import op

revision = "0010_log_retention"
down_revision = "0009_provider_node_launches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_logs_created", "logs", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_logs_created", table_name="logs")
