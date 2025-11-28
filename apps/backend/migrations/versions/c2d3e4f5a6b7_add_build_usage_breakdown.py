"""add_build_usage_breakdown_table

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2025-11-25 20:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "build_usage_breakdown",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("usage_record_id", sa.UUID(), nullable=False),
        sa.Column("deployment_id", sa.UUID(), nullable=False),
        sa.Column("build_minutes", sa.Float(), nullable=False, server_default="0.0"),
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
            "deployment_id",
            name="uq_build_breakdown_record_deployment",
        ),
    )
    op.create_index(
        "ix_build_usage_breakdown_usage_record_id",
        "build_usage_breakdown",
        ["usage_record_id"],
    )
    op.create_index(
        "ix_build_usage_breakdown_deployment_id",
        "build_usage_breakdown",
        ["deployment_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_build_usage_breakdown_deployment_id",
        table_name="build_usage_breakdown",
    )
    op.drop_index(
        "ix_build_usage_breakdown_usage_record_id",
        table_name="build_usage_breakdown",
    )
    op.drop_table("build_usage_breakdown")
