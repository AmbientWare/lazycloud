"""Bound forecast reads by recent platform arrivals."""

import sqlalchemy as sa
from alembic import op

revision = "0029_fleet_demand"
down_revision = "0028_capacity_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_containers_platform_arrivals",
        "containers",
        ["scheduling_requested_at"],
        postgresql_where=sa.text(
            "scheduling_placement = 'platform' AND scheduling_requested_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_containers_platform_arrivals", table_name="containers")
