"""Bind single-use bootstrap credentials to individual provider launches."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_provider_node_launches"
down_revision: str | None = "0008_placement_rate_classes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_node_launches",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("unit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("provider_ref", sa.String(255), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("server_name", sa.String(255), nullable=False),
        sa.Column("provider_instance_id", sa.String(128)),
        sa.Column("bootstrap_token_hash", sa.String(64), nullable=False),
        sa.Column("bootstrap_token_ciphertext", sa.Text()),
        sa.Column("node_token_hash", sa.String(64)),
        sa.Column("fingerprint_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("enrolled_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["compute_units.id"], ondelete="CASCADE"),
        sa.CheckConstraint("expires_at > created_at", name="ck_provider_node_launches_expiry"),
        sa.CheckConstraint("generation > 0", name="ck_provider_node_launches_generation"),
        sa.CheckConstraint(
            "(redeemed_at IS NULL AND node_token_hash IS NULL) OR "
            "(redeemed_at IS NOT NULL AND node_token_hash IS NOT NULL "
            "AND bootstrap_token_ciphertext IS NULL)",
            name="ck_provider_node_launches_redemption",
        ),
    )
    op.create_index(
        "uq_provider_node_launches_active_slot",
        "provider_node_launches",
        ["unit_id", "server_name"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_provider_node_launches_token_hash",
        "provider_node_launches",
        ["bootstrap_token_hash"],
        unique=True,
    )
    op.create_index(
        "uq_provider_node_launches_node_hash",
        "provider_node_launches",
        ["node_token_hash"],
        unique=True,
    )
    op.create_index(
        "uq_provider_node_launches_active_instance",
        "provider_node_launches",
        ["provider_ref", "provider_instance_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND provider_instance_id IS NOT NULL"),
        sqlite_where=sa.text("revoked_at IS NULL AND provider_instance_id IS NOT NULL"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM provider_node_launches)")):
        raise RuntimeError("cannot remove provider launch authorization while launch records exist")
    op.drop_table("provider_node_launches")
