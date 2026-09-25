"""Record the client claim id on a task attempt so a retried claim resumes it."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_task_attempt_claim"
down_revision = "0023_capacity_headroom"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_attempts", sa.Column("claim_id", postgresql.UUID(), nullable=True))
    op.create_index(
        "uq_task_attempts_container_claim",
        "task_attempts",
        ["container_id", "claim_id"],
        unique=True,
        postgresql_where=sa.text("claim_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_task_attempts_container_claim", table_name="task_attempts")
    op.drop_column("task_attempts", "claim_id")
