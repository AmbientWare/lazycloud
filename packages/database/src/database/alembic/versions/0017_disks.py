"""Add durable disks, their generations, a preferred scheduling worker, and credential seeds."""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "0017_disks"
down_revision = "0016_deployment_prunes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("credential_secret", sa.String(64), nullable=True))
    connection = op.get_bind()
    for (workspace_id,) in connection.execute(sa.text("SELECT id FROM workspaces")).all():
        connection.execute(
            sa.text("UPDATE workspaces SET credential_secret = :secret WHERE id = :id"),
            {"secret": secrets.token_hex(32), "id": workspace_id},
        )
    op.alter_column("workspaces", "credential_secret", nullable=False)
    op.create_table(
        "disks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column("holder_container_id", sa.UUID(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=False),
        sa.Column("last_worker_id", sa.Text(), nullable=False),
        sa.Column(
            "metered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("metered_bytes", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes > 0", name="ck_disks_size_positive"),
        sa.CheckConstraint("generation >= 0", name="ck_disks_generation_nonnegative"),
        sa.CheckConstraint("stored_bytes >= 0", name="ck_disks_stored_bytes_nonnegative"),
        sa.CheckConstraint("metered_bytes >= 0", name="ck_disks_metered_bytes_nonnegative"),
        sa.CheckConstraint(
            "(holder_container_id IS NULL) = (lease_token = '')",
            name="ck_disks_holder_has_lease",
        ),
        sa.CheckConstraint(
            "(status = 'deleting' AND deleted_at IS NOT NULL AND holder_container_id IS NULL) "
            "OR (status = 'attached' AND deleted_at IS NULL AND holder_container_id IS NOT NULL) "
            "OR (status = 'detached' AND deleted_at IS NULL AND holder_container_id IS NULL)",
            name="ck_disks_status",
        ),
    )
    op.create_index(
        "uq_disks_workspace_name_live",
        "disks",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_disks_metered_at_live",
        "disks",
        ["metered_at", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_disks_deleting",
        "disks",
        ["deleted_at", "id"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    op.create_index("ix_disks_holder", "disks", ["holder_container_id"])
    op.create_table(
        "disk_generations",
        sa.Column(
            "disk_id",
            sa.UUID(),
            sa.ForeignKey("disks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BigInteger(), primary_key=True),
        sa.Column("parent_generation", sa.BigInteger(), nullable=False),
        sa.Column("manifest_key", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("stored_bytes_added", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("generation > 0", name="ck_disk_generations_generation_positive"),
        sa.CheckConstraint(
            "parent_generation >= 0 AND parent_generation < generation",
            name="ck_disk_generations_parent_before",
        ),
        sa.CheckConstraint(
            "stored_bytes_added >= 0", name="ck_disk_generations_stored_bytes_nonnegative"
        ),
    )
    op.add_column(
        "containers",
        sa.Column(
            "scheduling_preferred_worker_id",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM disks) THEN RAISE EXCEPTION 'delete every disk before downgrading'; END IF; END $$"
    )
    op.drop_column("containers", "scheduling_preferred_worker_id")
    op.drop_table("disk_generations")
    op.drop_table("disks")
    op.drop_column("workspaces", "credential_secret")
