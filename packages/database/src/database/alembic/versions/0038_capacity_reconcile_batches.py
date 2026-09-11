"""Persist fair scheduling of bounded capacity reconciliation passes."""

import sqlalchemy as sa
from alembic import op

revision = "0038_capacity_reconcile_batches"
down_revision = "0037_remove_worker_rollout_surge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_units", sa.Column("provider_reconcile_attempt_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "compute_units", sa.Column("drain_reconcile_attempt_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("compute_units", "drain_reconcile_attempt_at")
    op.drop_column("compute_units", "provider_reconcile_attempt_at")
