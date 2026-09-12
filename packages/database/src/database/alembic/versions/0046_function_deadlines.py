"""Keep function execution deadlines on their owning task attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_function_deadlines"
down_revision = "0045_outbound_agent_tunnels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_callback_outbox",
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
            "task_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')", name="ck_task_callback_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_task_callback_attempts"),
        sa.CheckConstraint(
            "(status = 'sending') = (claim_token IS NOT NULL)", name="ck_task_callback_claim"
        ),
    )
    op.create_index(
        "uq_task_callback_identity", "task_callback_outbox", ["idempotency_key"], unique=True
    )
    op.create_index("ix_task_callback_ready", "task_callback_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_task_callback_stale", "task_callback_outbox", ["status", "claimed_at"])
    op.add_column("task_attempts", sa.Column("deadline_at", sa.DateTime(timezone=True)))
    op.create_index("ix_task_attempts_deadline", "task_attempts", ["deadline_at", "id"])
    op.execute("""
        WITH deadlines AS (
            SELECT a.id,
                t.started_at + make_interval(secs => COALESCE(
                    (s.payload #>> '{config,runtime,timeout_seconds}')::double precision,
                    3600
                )) AS deadline_at
            FROM task_attempts a
            JOIN tasks t ON t.id = a.task_id AND t.attempt_number = a.attempt_number
            JOIN stubs s ON s.id = t.stub_id
            WHERE t.status = 'running' AND a.status = 'running'
                AND s.type = 'function' AND t.started_at IS NOT NULL
                AND COALESCE(
                    (s.payload #>> '{config,runtime,timeout_seconds}')::double precision,
                    3600
                ) > 0
        )
        UPDATE task_attempts a
        SET deadline_at = d.deadline_at,
            payload = jsonb_set(a.payload, '{deadline_at}', to_jsonb(d.deadline_at))
        FROM deadlines d
        WHERE a.id = d.id
    """)


def downgrade() -> None:
    op.drop_table("task_callback_outbox")
    op.execute("UPDATE task_attempts SET payload = payload - 'deadline_at'")
    op.drop_index("ix_task_attempts_deadline", table_name="task_attempts")
    op.drop_column("task_attempts", "deadline_at")
