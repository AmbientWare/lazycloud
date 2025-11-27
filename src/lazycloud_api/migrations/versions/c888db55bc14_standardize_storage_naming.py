"""standardize_storage_naming

Revision ID: c888db55bc14
Revises: d3e4f5a6b7c8
Create Date: 2025-11-26 03:24:08.470530

Renames s3/efs columns to standard/shared in usage_records,
and converts s3-sc storage class values to ebs-sc in storage_usage_breakdown.
"""

from alembic import op
from sqlalchemy import text


revision: str = "c888db55bc14"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Rename columns in usage_records
    op.alter_column("usage_records", "s3_gb_hours", new_column_name="standard_gb_hours")
    op.alter_column("usage_records", "efs_gb_hours", new_column_name="shared_gb_hours")

    # Convert s3-sc to ebs-sc in storage_usage_breakdown
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'storage_usage_breakdown')"
        )
    )
    if result.scalar():
        op.execute(
            "UPDATE storage_usage_breakdown SET storage_class = 'ebs-sc' "
            "WHERE storage_class = 's3-sc'"
        )


def downgrade() -> None:
    # Rename columns back
    op.alter_column("usage_records", "standard_gb_hours", new_column_name="s3_gb_hours")
    op.alter_column("usage_records", "shared_gb_hours", new_column_name="efs_gb_hours")

    # Convert ebs-sc back to s3-sc (only for those that were originally s3-sc)
    conn = op.get_bind()
    result = conn.execute(
        text(
            "SELECT EXISTS (SELECT FROM information_schema.tables "
            "WHERE table_name = 'storage_usage_breakdown')"
        )
    )
    if result.scalar():
        op.execute(
            "UPDATE storage_usage_breakdown SET storage_class = 's3-sc' "
            "WHERE storage_class = 'ebs-sc'"
        )

