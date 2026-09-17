"""Persist interruption replacement ownership and due work."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_capacity_recovery"
down_revision = "0002_capacity_risk_advisory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=False)
    op.create_table(
        "capacity_recoveries",
        sa.Column("id", uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "workspace_id", uuid, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "source_unit_id",
            uuid,
            sa.ForeignKey("compute_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_machine_id", uuid, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("source_adjusted", sa.Boolean(), nullable=False),
        sa.Column("source_applied", sa.Boolean(), nullable=False),
        sa.Column("target_unit_id", uuid, sa.ForeignKey("compute_units.id", ondelete="RESTRICT")),
        sa.Column("operation_id", uuid),
        sa.Column("replacement_machine_id", uuid),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.CheckConstraint("attempt >= 0", name="ck_capacity_recoveries_attempt"),
        sa.CheckConstraint(
            "(target_unit_id IS NULL) = (operation_id IS NULL)",
            name="ck_capacity_recoveries_operation",
        ),
    )
    op.create_index(
        "uq_capacity_recoveries_source", "capacity_recoveries", ["source_machine_id"], unique=True
    )
    op.create_index(
        "ix_capacity_recoveries_due",
        "capacity_recoveries",
        ["next_action_at", "id"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_index("ix_capacity_recoveries_source_unit", "capacity_recoveries", ["source_unit_id"])
    op.create_index("ix_capacity_recoveries_target_unit", "capacity_recoveries", ["target_unit_id"])
    op.create_index(
        "uq_capacity_recoveries_replacement",
        "capacity_recoveries",
        ["replacement_machine_id"],
        unique=True,
        postgresql_where=sa.text("replacement_machine_id IS NOT NULL"),
    )
    op.execute("""
        INSERT INTO capacity_recoveries (
            workspace_id, source_unit_id, source_machine_id, observed_at, deadline,
            source_adjusted, source_applied, attempt, next_action_at, reason
        )
        SELECT DISTINCT ON (e.machine_id)
            u.workspace_id, u.id, e.machine_id, e.capacity_observed_at,
            e.capacity_notice_at, false, false, 0, now(), 'interruption requires recovery'
        FROM compute_machine_enrollments e
        JOIN compute_units u ON u.id = e.capacity_owner_id
        JOIN compute_provider_instances p ON p.machine_id = e.machine_id AND p.pool_id = u.id
        WHERE e.status = 'active'
            AND e.capacity_state IN ('draining', 'preempting', 'cordoned')
            AND e.capacity_notice_at IS NOT NULL
            AND e.capacity_observed_at IS NOT NULL
            AND p.status IN ('pending', 'active', 'terminating')
            AND u.phase NOT IN ('deleting', 'deleted')
        ORDER BY e.machine_id, e.capacity_observed_at
        ON CONFLICT (source_machine_id) DO NOTHING
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION enforce_capacity_recovery_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_machine_id IS DISTINCT FROM OLD.source_machine_id
        OR NEW.source_unit_id IS DISTINCT FROM OLD.source_unit_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR (OLD.source_adjusted AND NOT NEW.source_adjusted)
        OR (OLD.source_applied AND NOT NEW.source_applied)
        OR NEW.attempt < OLD.attempt
        OR (OLD.deadline IS NOT NULL AND (NEW.deadline IS NULL OR NEW.deadline > OLD.deadline))
        OR (OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION 'capacity recovery ownership cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER capacity_recovery_ownership_fence
BEFORE UPDATE ON capacity_recoveries
FOR EACH ROW EXECUTE FUNCTION enforce_capacity_recovery_ownership();
    """)


def downgrade() -> None:
    raise RuntimeError("capacity recovery ownership requires a forward migration")
