"""add_networking_usage_breakdown_table

Revision ID: b1c2d3e4f5a6
Revises: 033ca3a51574
Create Date: 2025-11-25 19:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "033ca3a51574"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "networking_usage_breakdown",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usage_record_id", sa.UUID(), nullable=False),
        sa.Column("deployment_id", sa.UUID(), nullable=True),
        sa.Column("service_name", sa.String(), nullable=False),
        sa.Column("endpoint_hours", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["usage_record_id"],
            ["usage_records.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["compose_deployments.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "usage_record_id",
            "service_name",
            name="uq_networking_breakdown_record_service",
        ),
    )
    op.create_index(
        "ix_networking_usage_breakdown_usage_record_id",
        "networking_usage_breakdown",
        ["usage_record_id"],
    )
    op.create_index(
        "ix_networking_usage_breakdown_deployment_id",
        "networking_usage_breakdown",
        ["deployment_id"],
    )
    op.create_index(
        "ix_networking_usage_breakdown_service_name",
        "networking_usage_breakdown",
        ["service_name"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_networking_usage_breakdown_service_name",
        table_name="networking_usage_breakdown",
    )
    op.drop_index(
        "ix_networking_usage_breakdown_deployment_id",
        table_name="networking_usage_breakdown",
    )
    op.drop_index(
        "ix_networking_usage_breakdown_usage_record_id",
        table_name="networking_usage_breakdown",
    )
    op.drop_table("networking_usage_breakdown")
