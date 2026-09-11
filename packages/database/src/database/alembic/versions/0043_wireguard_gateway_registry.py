"""Assign stable indices to independently addressed WireGuard gateways."""

import sqlalchemy as sa
from alembic import op

revision = "0043_wireguard_gateway_registry"
down_revision = "0042_worker_release_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wireguard_gateway",
        sa.Column("index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_unique_constraint("uq_wireguard_gateway_index", "wireguard_gateway", ["index"])
    op.create_unique_constraint(
        "uq_wireguard_gateway_public_key", "wireguard_gateway", ["public_key"]
    )
    op.create_check_constraint(
        "ck_wireguard_gateway_index", "wireguard_gateway", '"index" >= 0 AND "index" < 32'
    )


def downgrade() -> None:
    op.drop_constraint("ck_wireguard_gateway_index", "wireguard_gateway")
    op.drop_constraint("uq_wireguard_gateway_public_key", "wireguard_gateway")
    op.drop_constraint("uq_wireguard_gateway_index", "wireguard_gateway")
    op.drop_column("wireguard_gateway", "index")
