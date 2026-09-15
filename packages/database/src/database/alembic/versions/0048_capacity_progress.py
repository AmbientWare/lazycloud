from alembic import op

revision = "0048_capacity_progress"
down_revision = "0047_log_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_compute_capacity_operations_demand",
        "compute_capacity_operations",
        ["demand_container_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_compute_capacity_operations_demand", "compute_capacity_operations")
