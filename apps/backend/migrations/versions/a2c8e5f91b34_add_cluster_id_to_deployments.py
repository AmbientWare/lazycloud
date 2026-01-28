"""add cluster_id to deployments

Revision ID: a2c8e5f91b34
Revises: 7d5645aeecf3
Create Date: 2026-01-28 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c8e5f91b34"
down_revision: Union[str, None] = "7d5645aeecf3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add cluster_id column to compose_deployments table."""
    # Add column with server default for existing rows
    op.add_column(
        "compose_deployments",
        sa.Column(
            "cluster_id",
            sa.String(length=50),
            nullable=True,
            server_default="ash-1",
        ),
    )

    # Create index for cluster-based queries
    op.create_index(
        "ix_compose_deployments_cluster_id",
        "compose_deployments",
        ["cluster_id"],
        unique=False,
    )

    # Backfill existing rows (in case server_default doesn't apply to all)
    op.execute(
        "UPDATE compose_deployments SET cluster_id = 'ash-1' WHERE cluster_id IS NULL"
    )

    # Now make the column non-nullable and remove server default
    op.alter_column(
        "compose_deployments",
        "cluster_id",
        existing_type=sa.String(length=50),
        nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    """Remove cluster_id column from compose_deployments table."""
    op.drop_index("ix_compose_deployments_cluster_id", table_name="compose_deployments")
    op.drop_column("compose_deployments", "cluster_id")
