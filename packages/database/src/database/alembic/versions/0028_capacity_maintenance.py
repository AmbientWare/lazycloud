"""Reserve capacity for concurrent machine maintenance."""

import sqlalchemy as sa
from alembic import op

revision = "0028_capacity_maintenance"
down_revision = "0027_worker_release_rollout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capacity_maintenance",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("source_machine_id", sa.Uuid(), nullable=False),
        sa.Column("release_generation", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("surge_machines", sa.Integer(), nullable=False),
        sa.Column("running_cpu_millicores", sa.BigInteger(), nullable=False),
        sa.Column("hourly_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("replacement_machine_id", sa.Uuid(), nullable=True),
        sa.Column("reserved_cpu_millicores", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_memory_mib", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_gpu_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_disk_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_disk_volumes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["pool_id"], ["compute_units.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "kind IN ('runtime', 'reserve_refresh')", name="ck_capacity_maintenance_kind"
        ),
        sa.CheckConstraint(
            "phase IN ('planned', 'preparing', 'draining', 'verifying', 'retiring', 'complete', 'failed')",
            name="ck_capacity_maintenance_phase",
        ),
        sa.CheckConstraint(
            "release_generation > 0 AND surge_machines >= 0 AND running_cpu_millicores >= 0 "
            "AND (hourly_cost_micros IS NULL OR hourly_cost_micros >= 0)",
            name="ck_capacity_maintenance_commitments",
        ),
        sa.CheckConstraint(
            "(phase = 'complete') = (completed_at IS NOT NULL)",
            name="ck_capacity_maintenance_completion",
        ),
        sa.CheckConstraint(
            "reserved_cpu_millicores >= 0 AND reserved_memory_mib >= 0 "
            "AND reserved_gpu_count >= 0 AND reserved_disk_bytes >= 0 AND reserved_disk_volumes >= 0",
            name="ck_capacity_maintenance_reservations",
        ),
        sa.CheckConstraint(
            "replacement_machine_id IS NULL OR replacement_machine_id <> source_machine_id",
            name="ck_capacity_maintenance_replacement",
        ),
    )
    op.create_index(
        "uq_capacity_maintenance_source_generation",
        "capacity_maintenance",
        ["source_machine_id", "release_generation"],
        unique=True,
    )
    op.create_index(
        "uq_capacity_maintenance_source",
        "capacity_maintenance",
        ["source_machine_id"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_index(
        "uq_capacity_maintenance_replacement",
        "capacity_maintenance",
        ["replacement_machine_id"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NULL AND replacement_machine_id IS NOT NULL"),
    )
    op.create_index(
        "ix_capacity_maintenance_active_pool",
        "capacity_maintenance",
        ["pool_id"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_index("ix_capacity_maintenance_pool", "capacity_maintenance", ["pool_id"])
    op.execute("""
        INSERT INTO capacity_maintenance (
            pool_id, source_machine_id, release_generation, kind, phase,
            surge_machines, running_cpu_millicores, reason
        )
        SELECT id, replacement_machine_id::uuid, replacement_release_generation,
               'runtime', 'preparing', 1, worker_cpu_millicores, replacement_reason
        FROM compute_units WHERE replacement_release_generation > 0
    """)
    op.execute("""
        UPDATE compute_units SET replacement_machine_id = '', replacement_template_version = '',
            replacement_release_generation = 0
        WHERE replacement_release_generation > 0
    """)
    op.drop_index("ix_compute_units_release_replacement", table_name="compute_units")
    op.drop_constraint("ck_compute_units_release_replacement", "compute_units", type_="check")
    op.drop_column("compute_units", "replacement_release_generation")
    op.drop_column("compute_units", "replacement_reason")
    op.execute("""
CREATE OR REPLACE FUNCTION enforce_capacity_maintenance_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.completed_at IS NULL THEN
            RAISE EXCEPTION 'active capacity maintenance requires cleanup'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.source_machine_id IS DISTINCT FROM OLD.source_machine_id
        OR NEW.pool_id IS DISTINCT FROM OLD.pool_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.release_generation < OLD.release_generation
        OR (OLD.replacement_machine_id IS NOT NULL
            AND NEW.replacement_machine_id IS DISTINCT FROM OLD.replacement_machine_id
            AND (NEW.replacement_machine_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM compute_provider_instances
                WHERE machine_id = OLD.replacement_machine_id
                    AND status <> 'deleted'
            )))
        OR (OLD.completed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION 'capacity maintenance ownership cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER capacity_maintenance_ownership_fence
BEFORE UPDATE OR DELETE ON capacity_maintenance
FOR EACH ROW EXECUTE FUNCTION enforce_capacity_maintenance_ownership();
    """)


def downgrade() -> None:
    raise RuntimeError("capacity maintenance must be reconciled before restoring serial rollout")
