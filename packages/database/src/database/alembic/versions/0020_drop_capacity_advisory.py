"""Stop treating a provider rebalance advisory as a capacity state."""

from alembic import op

revision = "0020_drop_capacity_advisory"
down_revision = "0019_stub_power"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An open recovery started by an advisory is settled the way the recovery
    # owner finishes one without a replacement: its replacement operation is
    # released, and the source gets back what the recovery took from it.
    op.execute(
        """
        CREATE TEMP TABLE advisory_recoveries ON COMMIT DROP AS
        SELECT r.id, r.source_unit_id, r.target_unit_id, r.operation_id, r.source_adjusted
        FROM capacity_recoveries AS r
        JOIN compute_machine_enrollments AS e ON e.machine_id = r.source_machine_id
        WHERE r.completed_at IS NULL AND e.capacity_state = 'at_risk'
        """
    )
    # Releasing an operation that owns capacity lowers its unit's intent by one,
    # as `_release_pooled_capacity` does; the next provider reconcile applies it.
    op.execute(
        """
        UPDATE compute_units AS u
        SET desired_machines = GREATEST(u.min_machines, u.desired_machines - 1),
            generation = u.generation + 1,
            phase = 'updating',
            status = 'updating',
            updated_at = now()
        FROM compute_capacity_operations AS o
        JOIN advisory_recoveries AS a
            ON o.capacity_owner_id = a.target_unit_id AND o.operation_id = a.operation_id
        WHERE u.id = o.capacity_owner_id
            AND o.status NOT IN ('released', 'fulfilled', 'unsupported')
            AND o.owns_capacity
            AND o.release_desired_unit IS NULL
        """
    )
    op.execute(
        """
        UPDATE compute_capacity_operations AS o
        SET status = 'released',
            owns_capacity = false,
            release_desired_unit = CASE
                WHEN o.owns_capacity THEN COALESCE(o.release_desired_unit, u.desired_machines)
                ELSE o.release_desired_unit
            END,
            last_error = '',
            updated_at = now()
        FROM advisory_recoveries AS a, compute_units AS u
        WHERE o.capacity_owner_id = a.target_unit_id
            AND o.operation_id = a.operation_id
            AND u.id = o.capacity_owner_id
            AND o.status NOT IN ('released', 'fulfilled', 'unsupported')
        """
    )
    # `_adjust_source` lowered the source by one unless the recovery replaced a
    # paired surge, the one case whose target is the source itself, and marked
    # the source interrupted.
    op.execute(
        """
        UPDATE compute_units AS u
        SET desired_machines = CASE
                WHEN a.target_unit_id IS DISTINCT FROM a.source_unit_id
                THEN LEAST(u.desired_machines + 1, u.max_machines - u.stopped_machines)
                ELSE u.desired_machines
            END,
            degraded_reason = CASE
                WHEN u.degraded_reason = 'provider_interruption' THEN NULL
                ELSE u.degraded_reason
            END,
            degraded_at = CASE
                WHEN u.degraded_reason = 'provider_interruption' THEN NULL
                ELSE u.degraded_at
            END,
            phase = CASE WHEN u.phase = 'degraded' THEN 'updating' ELSE u.phase END,
            status = CASE WHEN u.phase = 'degraded' THEN 'updating' ELSE u.status END,
            generation = u.generation + 1,
            updated_at = now()
        FROM advisory_recoveries AS a
        WHERE u.id = a.source_unit_id
            AND a.source_adjusted
            AND u.phase NOT IN ('deleting', 'deleted')
        """
    )
    op.execute(
        """
        UPDATE capacity_recoveries AS r
        SET completed_at = now(), reason = 'advisory notices no longer start recovery'
        FROM advisory_recoveries AS a
        WHERE r.id = a.id
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
