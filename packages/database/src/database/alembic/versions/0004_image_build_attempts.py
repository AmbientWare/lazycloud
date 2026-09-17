"""Separate public image builds from fenced execution containers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_image_build_attempts"
down_revision = "0003_capacity_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=False)
    op.add_column("image_builds", sa.Column("execution_container_id", uuid))
    op.add_column(
        "image_builds",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "uq_image_builds_execution_container",
        "image_builds",
        ["execution_container_id"],
        unique=True,
    )
    op.create_check_constraint(
        "ck_image_builds_attempt_number", "image_builds", "attempt_number BETWEEN 0 AND 2"
    )
    op.create_table(
        "image_build_attempts",
        sa.Column("container_id", uuid, primary_key=True),
        sa.Column(
            "build_id", uuid, sa.ForeignKey("image_builds.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("cleanup_after", sa.DateTime(timezone=True)),
        sa.Column("log_sequence_base", sa.Integer(), nullable=False),
        sa.Column("upload_object_key", sa.Text()),
        sa.Column("upload_bucket", sa.String(255)),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("build_id", "number", name="uq_image_build_attempts_number"),
        sa.CheckConstraint("number BETWEEN 1 AND 2", name="ck_image_build_attempts_number"),
        sa.CheckConstraint("log_sequence_base >= 0", name="ck_image_build_attempts_log_base"),
    )
    op.create_index(
        "ix_image_build_attempts_cleanup_due",
        "image_build_attempts",
        ["cleanup_after"],
        postgresql_where=sa.text("cleanup_after IS NOT NULL"),
    )
    op.create_index(
        "ix_image_build_attempts_upload_cleanup",
        "image_build_attempts",
        ["upload_expires_at"],
        postgresql_where=sa.text("upload_object_key IS NOT NULL"),
    )
    op.execute("""
        INSERT INTO image_build_attempts (container_id, build_id, number, created_at, log_sequence_base)
        SELECT id, id, 1, created_at, 0 FROM image_builds
        WHERE (status IN ('pending', 'running') AND build_container_required IS TRUE)
            OR execution_cleanup_after IS NOT NULL
    """)
    op.execute("""
        UPDATE image_builds b SET execution_container_id = a.container_id, attempt_number = 1
        FROM image_build_attempts a WHERE a.build_id = b.id
    """)


def downgrade() -> None:
    raise RuntimeError("image execution ownership requires a forward migration")
