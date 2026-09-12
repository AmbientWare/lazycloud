"""Give serve previews independent lifetime and execution ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_preview_sessions"
down_revision = "0046_function_deadlines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preview_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_stub_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("stubs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "execution_stub_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("stubs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "container_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("containers.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("public", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('active', 'stopped', 'expired')", name="ck_preview_session_status"
        ),
        sa.CheckConstraint(
            "(status = 'active') = (ended_at IS NULL)", name="ck_preview_session_ended"
        ),
    )
    op.create_index(
        "uq_preview_session_execution_stub", "preview_sessions", ["execution_stub_id"], unique=True
    )
    op.create_index(
        "uq_preview_session_container", "preview_sessions", ["container_id"], unique=True
    )
    op.create_index("ix_preview_session_status", "preview_sessions", ["status", "id"])
    op.create_index("ix_preview_session_workspace", "preview_sessions", ["workspace_id", "id"])


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM preview_sessions p
                LEFT JOIN containers c ON c.id = p.container_id
                WHERE p.status = 'active' OR c.status IN ('pending', 'running')
            ) OR EXISTS (
                SELECT 1 FROM deployments d JOIN preview_sessions p
                ON d.stub_id = p.execution_stub_id
            ) THEN
                RAISE EXCEPTION 'stop previews and remove deployments of preview executions before downgrade';
            END IF;
        END $$;
    """)
    op.execute("DELETE FROM stubs WHERE id IN (SELECT execution_stub_id FROM preview_sessions)")
    op.drop_table("preview_sessions")
