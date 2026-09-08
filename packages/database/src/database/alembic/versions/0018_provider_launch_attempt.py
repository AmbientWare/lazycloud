"""Fence provider creates whose APIs have no idempotency key."""

import sqlalchemy as sa
from alembic import op

revision = "0018_provider_launch_attempt"
down_revision = "0017_managed_compute_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_node_launches",
        sa.Column("creation_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_node_launches",
        sa.Column("provider_operation_id", sa.String(128), nullable=True),
    )
    op.create_index(
        "uq_provider_node_launches_active_operation",
        "provider_node_launches",
        ["provider_ref", "provider_operation_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND provider_operation_id IS NOT NULL"),
    )
    op.add_column(
        "provider_node_launches",
        sa.Column("provider_resource_id", sa.String(128), nullable=True),
    )
    op.create_index(
        "uq_provider_node_launches_active_resource",
        "provider_node_launches",
        ["provider_ref", "provider_resource_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND provider_resource_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_provider_node_launches_active_resource", "provider_node_launches")
    op.drop_column("provider_node_launches", "provider_resource_id")
    op.drop_index("uq_provider_node_launches_active_operation", "provider_node_launches")
    op.drop_column("provider_node_launches", "provider_operation_id")
    op.drop_column("provider_node_launches", "creation_attempted_at")
