"""Constrain durable capacity outcomes and require named fulfillment."""

from alembic import op

revision = "0039_capacity_operation_outcomes"
down_revision = "0038_capacity_reconcile_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_compute_capacity_operations_status",
        "compute_capacity_operations",
        "status IN ('intent', 'existing_pending', 'requested', 'at_limit', "
        "'temporarily_unavailable', 'rejected', 'unsupported', 'releasing', "
        "'released', 'fulfilled')",
    )
    op.create_check_constraint(
        "ck_compute_capacity_operations_fulfillment",
        "compute_capacity_operations",
        "status <> 'fulfilled' OR target_machine_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_compute_capacity_operations_fulfillment", "compute_capacity_operations", type_="check"
    )
    op.drop_constraint(
        "ck_compute_capacity_operations_status", "compute_capacity_operations", type_="check"
    )
