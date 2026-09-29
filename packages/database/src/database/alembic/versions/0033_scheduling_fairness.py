"""Persist the admission account used for scheduling fairness."""

import sqlalchemy as sa
from alembic import op

revision = "0033_scheduling_fairness"
down_revision = "0032_capacity_sleep_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("containers", sa.Column("scheduling_fairness_account_id", sa.Text()))
    op.execute("""
        UPDATE containers c
        SET scheduling_fairness_account_id = coalesce(
            (SELECT m.user_id::text FROM workspace_members m
             WHERE m.workspace_id = c.workspace_id AND m.role = 'owner'),
            c.workspace_id::text
        )
        WHERE c.scheduling_requested_at IS NOT NULL
    """)
    op.create_check_constraint(
        "ck_containers_scheduling_fairness",
        "containers",
        "scheduling_requested_at IS NULL OR "
        "(scheduling_fairness_account_id IS NOT NULL "
        "AND length(scheduling_fairness_account_id) > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_containers_scheduling_fairness", "containers", type_="check")
    op.drop_column("containers", "scheduling_fairness_account_id")
