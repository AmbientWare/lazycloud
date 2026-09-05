"""Reserve rollout capacity and fence container work admission."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_worker_rollout_drains"
down_revision: str | None = "0005_image_build_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "containers", sa.Column("workload_ready_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "compute_units",
        sa.Column("worker_rollout_surge", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "container_rollout_drains",
        sa.Column(
            "container_id",
            sa.Uuid(),
            sa.ForeignKey("containers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "stub_id", sa.Uuid(), sa.ForeignKey("stubs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("serving_floor", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admission_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("serving_floor > 0", name="ck_container_rollout_drains_serving_floor"),
    )
    op.create_index("ix_container_rollout_drains_stub", "container_rollout_drains", ["stub_id"])


def downgrade() -> None:
    op.drop_table("container_rollout_drains")
    op.drop_column("containers", "workload_ready_at")
    op.drop_column("compute_units", "worker_rollout_surge")
