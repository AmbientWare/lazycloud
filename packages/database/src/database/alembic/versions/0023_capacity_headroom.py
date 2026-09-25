"""Plan platform reserves by headroom: GPU reserves, node memory, live-row indexes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_capacity_headroom"
down_revision = "0022_disk_unavailable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_compute_units_stopped_capacity", "compute_units", type_="check")
    op.create_check_constraint(
        "ck_compute_units_stopped_capacity",
        "compute_units",
        "stopped_machines >= 0 AND retiring_stopped_machines >= 0 "
        "AND desired_machines + stopped_machines <= max_machines "
        "AND (stopped_machines = 0 OR platform_fleet)",
    )
    op.drop_column("compute_units", "warm_handoff_from")
    op.create_table(
        "compute_node_shapes",
        sa.Column("cpu_millicores", sa.Integer(), primary_key=True),
        sa.Column("memory_mib", sa.Integer(), primary_key=True),
        sa.Column("gpu_count", sa.Integer(), primary_key=True),
        sa.Column("reported_memory_mib", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("reported_memory_mib > 0", name="ck_compute_node_shapes_memory"),
    )
    op.execute(
        "INSERT INTO compute_node_shapes "
        "(cpu_millicores, memory_mib, gpu_count, reported_memory_mib) "
        "SELECT unit.worker_cpu_millicores, unit.worker_memory_mib, unit.worker_gpu_count, "
        "min(enrollment.memory_mb) "
        "FROM compute_machine_enrollments AS enrollment "
        "JOIN compute_units AS unit ON unit.id = enrollment.capacity_owner_id "
        "WHERE unit.visibility = 'internal' AND enrollment.memory_mb > 0 "
        "GROUP BY 1, 2, 3"
    )
    op.create_index(
        "ix_compute_units_platform_live",
        "compute_units",
        ["id"],
        postgresql_where=sa.text(
            "platform_fleet IS TRUE AND visibility = 'internal' AND phase <> 'deleted'"
        ),
    )
    op.create_index(
        "ix_compute_provider_instances_live",
        "compute_provider_instances",
        ["pool_id"],
        postgresql_where=sa.text("status NOT IN ('deleted', 'failed')"),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM compute_units WHERE worker_gpu_count > 0 "
        "AND (stopped_machines > 0 OR retiring_stopped_machines > 0)) THEN "
        "RAISE EXCEPTION 'retire stopped GPU reserves before downgrading'; END IF; END $$"
    )
    op.drop_index("ix_compute_provider_instances_live", table_name="compute_provider_instances")
    op.drop_index("ix_compute_units_platform_live", table_name="compute_units")
    op.drop_table("compute_node_shapes")
    op.add_column(
        "compute_units",
        sa.Column(
            "warm_handoff_from",
            postgresql.ARRAY(postgresql.UUID()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.drop_constraint("ck_compute_units_stopped_capacity", "compute_units", type_="check")
    op.create_check_constraint(
        "ck_compute_units_stopped_capacity",
        "compute_units",
        "stopped_machines >= 0 AND retiring_stopped_machines >= 0 "
        "AND desired_machines + stopped_machines <= max_machines AND (stopped_machines = 0 OR "
        "(platform_fleet AND worker_gpu_count = 0))",
    )
