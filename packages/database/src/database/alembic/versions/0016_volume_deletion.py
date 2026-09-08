"""Keep volume deletion intent until storage cleanup completes."""

import sqlalchemy as sa
from alembic import op

revision = "0016_volume_deletion"
down_revision = "0015_unimplemented_hooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("containers", sa.Column("storage_released_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_containers_pending_storage_worker",
        "containers",
        [sa.text("(payload ->> 'runtime_worker_id')"), "id"],
        postgresql_where=sa.text("storage_released_at IS NULL"),
    )
    op.add_column("volumes", sa.Column("deletion_requested_at", sa.DateTime(timezone=True)))
    op.add_column(
        "volumes",
        sa.Column(
            "unfenced_writes_possible", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.alter_column("volumes", "unfenced_writes_possible", server_default=None)
    op.create_index("ix_volumes_deletion_requested_at", "volumes", ["deletion_requested_at", "id"])
    op.create_table(
        "volume_cleanup",
        sa.Column("volume_id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("swept_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_volume_cleanup_swept_at", "volume_cleanup", ["swept_at", "volume_id"])


def downgrade() -> None:
    op.drop_index("ix_containers_pending_storage_worker", table_name="containers")
    op.drop_column("containers", "storage_released_at")
    op.drop_index("ix_volumes_deletion_requested_at", table_name="volumes")
    op.drop_table("volume_cleanup")
    op.drop_column("volumes", "unfenced_writes_possible")
    op.drop_column("volumes", "deletion_requested_at")
