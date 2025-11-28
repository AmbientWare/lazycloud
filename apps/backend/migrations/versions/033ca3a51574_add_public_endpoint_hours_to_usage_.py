"""add_public_endpoint_hours_to_usage_records

Revision ID: 033ca3a51574
Revises: 450c39bbeff1
Create Date: 2025-11-25 11:36:44.215560

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "033ca3a51574"
down_revision: Union[str, None] = "450c39bbeff1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "usage_records",
        sa.Column(
            "public_endpoint_hours", sa.Float(), nullable=False, server_default="0.0"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("usage_records", "public_endpoint_hours")
