"""add_current_task_run_id_to_deployments

Revision ID: 285c3c95198a
Revises: 54d3ce48cb59
Create Date: 2025-11-23 18:36:09.778845

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "285c3c95198a"
down_revision: Union[str, None] = "54d3ce48cb59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "compose_deployments",
        sa.Column("current_task_run_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_compose_deployments_current_task_run_id",
        "compose_deployments",
        ["current_task_run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_compose_deployments_current_task_run_id", table_name="compose_deployments"
    )
    op.drop_column("compose_deployments", "current_task_run_id")
