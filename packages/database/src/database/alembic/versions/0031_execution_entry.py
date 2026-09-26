"""Record execution-entry bounds on durable task attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0031_execution_entry"
down_revision = "0030_capacity_activations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_attempts",
        sa.Column("execution_entry_upper_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_attempts",
        sa.Column("execution_entry_reported_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_attempts", "execution_entry_reported_at")
    op.drop_column("task_attempts", "execution_entry_upper_bound_at")
