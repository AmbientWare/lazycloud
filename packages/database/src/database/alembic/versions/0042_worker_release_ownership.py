"""Persist verified worker releases and unfinished in-place updates."""

import sqlalchemy as sa
from alembic import op

revision = "0042_worker_release_ownership"
down_revision = "0041_capacity_ownership_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "admitted_release_generation", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "workers",
        sa.Column("admitted_runtime_image", sa.String(1024), nullable=False, server_default=""),
    )
    op.add_column(
        "workers",
        sa.Column("admitted_agent_sha256", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "workers",
        sa.Column("update_generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "workers",
        sa.Column("update_runtime_image", sa.String(1024), nullable=False, server_default=""),
    )
    op.add_column(
        "workers",
        sa.Column("update_agent_sha256", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column("workers", sa.Column("update_started_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_workers_release_generation", "workers", "admitted_release_generation >= 0"
    )
    op.create_check_constraint("ck_workers_update_generation", "workers", "update_generation >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_workers_update_generation", "workers")
    op.drop_constraint("ck_workers_release_generation", "workers")
    for name in (
        "update_started_at",
        "update_agent_sha256",
        "update_runtime_image",
        "update_generation",
        "admitted_agent_sha256",
        "admitted_runtime_image",
        "admitted_release_generation",
    ):
        op.drop_column("workers", name)
