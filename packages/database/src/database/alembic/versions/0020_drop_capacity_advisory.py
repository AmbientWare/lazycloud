"""Stop treating a provider rebalance advisory as a capacity state."""

from alembic import op

revision = "0020_drop_capacity_advisory"
down_revision = "0019_stub_power"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE capacity_recoveries AS r
        SET completed_at = now(), reason = 'advisory notices no longer start recovery'
        FROM compute_machine_enrollments AS e
        WHERE r.completed_at IS NULL
            AND e.machine_id = r.source_machine_id
            AND e.capacity_state = 'at_risk'
        """
    )
    op.execute(
        """
        UPDATE compute_machine_enrollments
        SET capacity_state = 'available', capacity_reason = ''
        WHERE capacity_state = 'at_risk'
        """
    )
    op.drop_constraint(
        "ck_compute_machine_enrollments_capacity_state",
        "compute_machine_enrollments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_compute_machine_enrollments_capacity_state",
        "compute_machine_enrollments",
        "capacity_state IN ('available', 'draining', 'preempting', 'cordoned')",
    )


def downgrade() -> None:
    raise RuntimeError("the capacity risk advisory state requires a forward migration")
