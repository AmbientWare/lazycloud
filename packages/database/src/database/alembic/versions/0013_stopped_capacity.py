"""Retained CPU reserve capacity and restartable machine lifecycles."""

import sqlalchemy as sa
from alembic import op

revision = "0013_stopped_capacity"
down_revision = "0012_machine_lifecycle"
branch_labels = None
depends_on = None

_PREVIOUS = (
    "'requested', 'provisioning', 'booting', 'joining', 'ready', "
    "'draining', 'terminating', 'deleted', 'failed'"
)


def upgrade() -> None:
    op.create_index(
        "ix_compute_provider_instances_stopped",
        "compute_provider_instances",
        ["pool_id"],
        postgresql_where=sa.text("status = 'stopped' AND missing_since IS NULL"),
    )
    op.create_index(
        "ix_worker_cache_generations_storage_latest",
        "worker_cache_generations",
        ["storage_id", "updated_at", "id"],
    )
    op.add_column(
        "compute_units",
        sa.Column("retiring_stopped_machines", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "compute_units",
        sa.Column("stopped_machines", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_compute_units_stopped_capacity",
        "compute_units",
        "stopped_machines >= 0 AND retiring_stopped_machines >= 0 "
        "AND desired_machines + stopped_machines <= max_machines AND (stopped_machines = 0 OR "
        "(platform_fleet AND NOT worker_preemptible AND worker_gpu_count = 0))",
    )
    op.drop_constraint("ck_machines_lifecycle", "machines", type_="check")
    op.create_check_constraint(
        "ck_machines_lifecycle",
        "machines",
        f"lifecycle IN ({_PREVIOUS}, 'stopping', 'stopped', 'resuming')",
    )


def downgrade() -> None:
    # Retained instances must be retired before their durable intent can be removed.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM compute_units WHERE stopped_machines > 0 OR retiring_stopped_machines > 0) "
        "OR EXISTS (SELECT 1 FROM machines WHERE lifecycle IN "
        "('stopping', 'stopped', 'resuming')) THEN "
        "RAISE EXCEPTION 'retire stopped capacity before downgrading'; END IF; END $$"
    )
    op.drop_constraint("ck_machines_lifecycle", "machines", type_="check")
    op.create_check_constraint("ck_machines_lifecycle", "machines", f"lifecycle IN ({_PREVIOUS})")
    op.drop_constraint("ck_compute_units_stopped_capacity", "compute_units", type_="check")
    op.drop_column("compute_units", "stopped_machines")
    op.drop_column("compute_units", "retiring_stopped_machines")
    op.drop_index("ix_compute_provider_instances_stopped", table_name="compute_provider_instances")
    op.drop_index(
        "ix_worker_cache_generations_storage_latest", table_name="worker_cache_generations"
    )
