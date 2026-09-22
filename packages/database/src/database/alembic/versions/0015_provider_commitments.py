"""Keep unresolved provider operations in fleet capacity accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0015_provider_commitments"
down_revision = "0014_stopped_purchase_markets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_units",
        sa.Column(
            "provider_committed_machines", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_compute_units_provider_commitment", "compute_units", "provider_committed_machines >= 0"
    )
    op.execute(
        "UPDATE compute_units SET provider_committed_machines = "
        "jsonb_array_length(provider_attributes->'slots') "
        "WHERE provider_resource_id LIKE 'ec2-pool-%' "
        "AND jsonb_typeof(provider_attributes->'slots') = 'array'"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM compute_units "
        "WHERE provider_committed_machines > 0) THEN "
        "RAISE EXCEPTION 'finish provider commitments before downgrading'; END IF; END $$"
    )
    op.drop_constraint("ck_compute_units_provider_commitment", "compute_units", type_="check")
    op.drop_column("compute_units", "provider_committed_machines")
