"""Record provider snapshots of disk volumes and the snapshot a volume starts from."""

import sqlalchemy as sa
from alembic import op

revision = "0022_disk_snapshots"
down_revision = "0021_reserve_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "disks",
        sa.Column(
            "volume_source_snapshot_id",
            sa.String(64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    # The default only fills existing rows; the application writes every new one.
    op.alter_column("disks", "volume_source_snapshot_id", server_default=None)
    op.create_table(
        "disk_snapshots",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
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
        sa.Column(
            "disk_id",
            sa.UUID(),
            sa.ForeignKey("disks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("provider_ref", sa.String(160), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=True),
        sa.Column("capacity_workspace_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("volume_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "disk_id",
            "generation",
            "provider_ref",
            "region",
            name="uq_disk_snapshots_generation_scope",
        ),
        sa.UniqueConstraint("token", name="uq_disk_snapshots_token"),
        sa.CheckConstraint(
            "state IN ('creating', 'pending', 'completed', 'deleting')",
            name="ck_disk_snapshots_state",
        ),
        sa.CheckConstraint(
            "(state = 'completed') = (due_at IS NULL)", name="ck_disk_snapshots_due"
        ),
        sa.CheckConstraint(
            "state = 'creating' OR snapshot_id <> ''", name="ck_disk_snapshots_snapshot_id"
        ),
        sa.CheckConstraint("generation > 0", name="ck_disk_snapshots_generation_positive"),
        sa.CheckConstraint("volume_size_bytes > 0", name="ck_disk_snapshots_volume_size_positive"),
        sa.CheckConstraint("stored_bytes >= 0", name="ck_disk_snapshots_stored_bytes_nonnegative"),
    )
    op.create_index(
        "ix_disk_snapshots_due",
        "disk_snapshots",
        ["due_at", "id"],
        postgresql_where=sa.text("state IN ('creating', 'pending', 'deleting')"),
    )
    op.create_index(
        "ix_disk_snapshots_connection",
        "disk_snapshots",
        ["connection_id"],
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("disk_snapshots")
    op.drop_column("disks", "volume_source_snapshot_id")
