"""Persist scheduling requests and capacity demand across scheduler restarts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040_container_capacity_demand"
down_revision = "0039_capacity_operation_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("containers", sa.Column("scheduling_request", postgresql.JSONB()))
    op.add_column("containers", sa.Column("scheduling_reconcile_at", sa.DateTime(timezone=True)))
    op.add_column("containers", sa.Column("scheduling_assigned_at", sa.DateTime(timezone=True)))
    op.add_column("containers", sa.Column("scheduling_assignment_token", sa.String(240)))
    op.add_column("containers", sa.Column("capacity_retry_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_containers_capacity_due",
        "containers",
        ["capacity_retry_at", "id"],
        postgresql_where=sa.text("capacity_retry_at IS NOT NULL AND status = 'pending'"),
    )
    op.create_index(
        "ix_containers_scheduling_due",
        "containers",
        ["scheduling_reconcile_at", "id"],
        postgresql_where=sa.text("scheduling_request IS NOT NULL AND status = 'pending'"),
    )

    op.execute("""
CREATE OR REPLACE FUNCTION enforce_container_assignment_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.status IN ('exited', 'failed', 'stopped') AND NEW.status IN ('pending', 'running'))
        OR (OLD.status = 'running' AND NEW.status = 'pending') THEN
        RAISE EXCEPTION 'container status cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    IF COALESCE(OLD.payload->>'runtime_worker_id', '') <> '' AND (
        COALESCE(NEW.payload->>'runtime_worker_id', '')
            <> COALESCE(OLD.payload->>'runtime_worker_id', '')
        OR COALESCE(NEW.payload->>'runtime_machine_id', '')
            <> COALESCE(OLD.payload->>'runtime_machine_id', '')
    ) AND NOT (
        OLD.status = 'pending' AND NEW.status = 'pending'
        AND COALESCE(NEW.payload->>'runtime_worker_id', '') = ''
        AND COALESCE(NEW.payload->>'runtime_machine_id', '') = ''
        AND OLD.scheduling_assignment_token IS NOT NULL
        AND NEW.scheduling_assignment_token IS NULL
    ) THEN
        RAISE EXCEPTION 'container assignment cannot change without its ownership token'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER container_assignment_ownership_fence
BEFORE UPDATE ON containers
FOR EACH ROW EXECUTE FUNCTION enforce_container_assignment_ownership();
""")


def downgrade() -> None:
    op.execute("DROP TRIGGER container_assignment_ownership_fence ON containers")
    op.execute("DROP FUNCTION enforce_container_assignment_ownership()")
    op.drop_index("ix_containers_capacity_due", table_name="containers")
    op.drop_index("ix_containers_scheduling_due", table_name="containers")
    op.drop_column("containers", "capacity_retry_at")
    op.drop_column("containers", "scheduling_reconcile_at")
    op.drop_column("containers", "scheduling_assigned_at")
    op.drop_column("containers", "scheduling_assignment_token")
    op.drop_column("containers", "scheduling_request")
