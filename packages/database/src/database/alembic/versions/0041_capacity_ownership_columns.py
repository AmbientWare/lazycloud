"""Keep capacity handoff and acquisition attribution in relational columns."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041_capacity_ownership_columns"
down_revision = "0040_container_capacity_demand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_units",
        sa.Column("warm_handoff_from", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("compute_capacity_operations", sa.Column("demand_container_id", sa.String(160)))
    op.add_column(
        "compute_capacity_operations", sa.Column("fulfilled_at", sa.DateTime(timezone=True))
    )
    op.execute("""
CREATE OR REPLACE FUNCTION enforce_compute_capacity_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('released', 'fulfilled', 'unsupported') AND (
        NEW.status IS DISTINCT FROM OLD.status
        OR NEW.target_machine_id IS DISTINCT FROM OLD.target_machine_id
        OR (
            NOT COALESCE((OLD.payload->>'owns_capacity')::boolean, false)
            AND COALESCE((NEW.payload->>'owns_capacity')::boolean, false)
        )
    ) THEN
        RAISE EXCEPTION 'terminal capacity ownership cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER compute_capacity_ownership_fence
BEFORE UPDATE ON compute_capacity_operations
FOR EACH ROW EXECUTE FUNCTION enforce_compute_capacity_ownership();
""")


def downgrade() -> None:
    op.execute("DROP TRIGGER compute_capacity_ownership_fence ON compute_capacity_operations")
    op.execute("DROP FUNCTION enforce_compute_capacity_ownership()")
    op.drop_column("compute_capacity_operations", "fulfilled_at")
    op.drop_column("compute_capacity_operations", "demand_container_id")
    op.drop_column("compute_units", "warm_handoff_from")
