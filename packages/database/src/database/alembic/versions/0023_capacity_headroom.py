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
    op.add_column(
        "compute_units",
        sa.Column("node_memory_mib", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE compute_units AS unit SET node_memory_mib = shape.memory "
        "FROM (SELECT owner.worker_cpu_millicores AS cpu, owner.worker_memory_mib AS mem, "
        "owner.worker_gpu_count AS gpu, min(enrollment.memory_mb) AS memory "
        "FROM compute_units AS owner JOIN compute_machine_enrollments AS enrollment "
        "ON enrollment.capacity_owner_id = owner.id "
        "WHERE owner.visibility = 'internal' AND enrollment.memory_mb > 0 "
        "GROUP BY 1, 2, 3) AS shape "
        "WHERE unit.worker_cpu_millicores = shape.cpu AND unit.worker_memory_mib = shape.mem "
        "AND unit.worker_gpu_count = shape.gpu"
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
    op.drop_column("compute_units", "node_memory_mib")
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
