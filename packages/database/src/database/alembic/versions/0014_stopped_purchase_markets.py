"""Allow platform CPU reserves in either purchase market."""

import sqlalchemy as sa
from alembic import op

revision = "0014_stopped_purchase_markets"
down_revision = "0013_stopped_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_units",
        sa.Column("provider_state_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_compute_units_provider_revision", "compute_units", "provider_state_revision >= 0"
    )
    op.drop_constraint("ck_compute_units_stopped_capacity", "compute_units", type_="check")
    op.create_check_constraint(
        "ck_compute_units_stopped_capacity",
        "compute_units",
        "stopped_machines >= 0 AND retiring_stopped_machines >= 0 "
        "AND desired_machines + stopped_machines <= max_machines AND (stopped_machines = 0 OR "
        "(platform_fleet AND worker_gpu_count = 0))",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM compute_units WHERE provider_state_revision > 0 "
        "AND provider_resource_id <> '') THEN "
        "RAISE EXCEPTION 'retire persistent capacity pools before downgrading'; END IF; "
        "IF EXISTS (SELECT 1 FROM compute_units WHERE worker_preemptible "
        "AND (stopped_machines > 0 OR retiring_stopped_machines > 0)) THEN "
        "RAISE EXCEPTION 'retire stopped Spot capacity before downgrading'; END IF; END $$"
    )
    op.drop_constraint("ck_compute_units_stopped_capacity", "compute_units", type_="check")
    op.create_check_constraint(
        "ck_compute_units_stopped_capacity",
        "compute_units",
        "stopped_machines >= 0 AND retiring_stopped_machines >= 0 "
        "AND desired_machines + stopped_machines <= max_machines AND (stopped_machines = 0 OR "
        "(platform_fleet AND NOT worker_preemptible AND worker_gpu_count = 0))",
    )
    op.drop_constraint("ck_compute_units_provider_revision", "compute_units", type_="check")
    op.drop_column("compute_units", "provider_state_revision")
