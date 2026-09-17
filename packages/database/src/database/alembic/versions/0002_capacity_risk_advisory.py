"""Distinguish provider risk advisories from interruption deadlines."""

from alembic import op

revision = "0002_capacity_risk_advisory"
down_revision = "0001_relational_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_compute_machine_enrollments_capacity_state",
        "compute_machine_enrollments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_compute_machine_enrollments_capacity_state",
        "compute_machine_enrollments",
        "capacity_state IN ('available', 'at_risk', 'draining', 'preempting', 'cordoned')",
    )


def downgrade() -> None:
    raise RuntimeError("capacity risk state requires a forward migration")
