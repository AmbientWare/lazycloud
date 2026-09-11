"""Worker software updates do not reserve provider machines."""

import sqlalchemy as sa
from alembic import op

revision = "0037_remove_worker_rollout_surge"
down_revision = "0036_idle_container_drains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("compute_units", "worker_rollout_surge")


def downgrade() -> None:
    op.add_column(
        "compute_units",
        sa.Column("worker_rollout_surge", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
