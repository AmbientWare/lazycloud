"""Record the release a stopped reserve machine was prepared with."""

import sqlalchemy as sa
from alembic import op

revision = "0021_reserve_release"
down_revision = "0020_drop_capacity_advisory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("prepared_agent_sha256", "prepared_worker_image"):
        op.add_column(
            "compute_provider_instances",
            sa.Column(column, sa.Text(), nullable=False, server_default=sa.text("''")),
        )


def downgrade() -> None:
    op.drop_column("compute_provider_instances", "prepared_worker_image")
    op.drop_column("compute_provider_instances", "prepared_agent_sha256")
