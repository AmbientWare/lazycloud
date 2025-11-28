"""Add depot_project_id to compose_deployments

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2025-01-15 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "compose_deployments",
        sa.Column("depot_project_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_compose_deployments_depot_project_id",
        "compose_deployments",
        ["depot_project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_compose_deployments_depot_project_id",
        table_name="compose_deployments",
    )
    op.drop_column("compose_deployments", "depot_project_id")
