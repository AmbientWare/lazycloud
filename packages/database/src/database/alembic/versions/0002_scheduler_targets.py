"""Replace scheduler catalog scans with a durable due-target queue.

Revision ID: 0002_scheduler_targets
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_scheduler_targets"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_stubs_reusable_identity",
        "stubs",
        ["workspace_id", "name", "app_id", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text("CAST((payload ->> 'deployment_id') AS VARCHAR) IS NULL"),
        sqlite_where=sa.text("JSON_EXTRACT(payload, '$.\"deployment_id\"') IS NULL"),
    )
    op.create_table(
        "autoscaling_targets",
        sa.Column("stub_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("target_kind", sa.String(length=80), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("generation >= 1", name="ck_autoscaling_targets_generation"),
        sa.CheckConstraint(
            "target_kind IN ('function', 'endpoint', 'pod')",
            name="ck_autoscaling_targets_kind",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (claim_expires_at IS NULL)",
            name="ck_autoscaling_targets_claim",
        ),
        sa.ForeignKeyConstraint(["stub_id"], ["stubs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("stub_id"),
    )
    op.create_index(
        "ix_autoscaling_targets_due",
        "autoscaling_targets",
        ["due_at", "stub_id"],
        unique=False,
    )
    op.create_index(
        "ix_autoscaling_targets_workspace",
        "autoscaling_targets",
        ["workspace_id"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        configured_capacity = """
            COALESCE(CAST(s.payload -> 'config' -> 'autoscaler' ->> 'min_containers' AS INTEGER), 0) > 0
            OR COALESCE(CAST(s.payload -> 'config' -> 'runtime' ->> 'keep_warm' AS INTEGER), 0) = -1
        """
    else:
        configured_capacity = """
            COALESCE(CAST(json_extract(s.payload, '$.config.autoscaler.min_containers') AS INTEGER), 0) > 0
            OR COALESCE(CAST(json_extract(s.payload, '$.config.runtime.keep_warm') AS INTEGER), 0) = -1
        """
    op.execute(
        sa.text(
            f"""
            INSERT INTO autoscaling_targets (
                stub_id,
                workspace_id,
                target_kind,
                due_at,
                generation,
                created_at,
                updated_at
            )
            SELECT
                s.id,
                s.workspace_id,
                CASE
                    WHEN s.type = 'function' THEN 'function'
                    WHEN s.type IN ('endpoint', 'asgi') THEN 'endpoint'
                    ELSE 'pod'
                END,
                CURRENT_TIMESTAMP,
                1,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM stubs AS s
            WHERE s.type IN ('function', 'endpoint', 'asgi', 'pod', 'sandbox')
              AND (
                  EXISTS (
                      SELECT 1
                      FROM containers AS c
                      WHERE c.stub_id = s.id
                        AND c.status IN ('pending', 'running')
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM tasks AS t
                      WHERE t.stub_id = s.id
                        AND t.status = 'pending'
                        AND t.container_id IS NULL
                        AND t.claimable_at IS NOT NULL
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM endpoint_dispatches AS d
                      WHERE d.stub_id = s.id
                        AND d.status IN ('queued', 'waiting-capacity', 'inflight')
                  )
                  OR {configured_capacity}
              )
            """
        )
    )
    op.drop_index("ix_containers_stub_autoscaling", table_name="containers")
    op.create_index(
        "ix_containers_stub_live",
        "containers",
        ["stub_id", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_containers_stub_failed_created",
        "containers",
        ["stub_id", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'failed'"),
        sqlite_where=sa.text("status = 'failed'"),
    )
    op.create_index(
        "ix_containers_stub_failed_finished",
        "containers",
        ["stub_id", "finished_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'failed' AND finished_at IS NOT NULL"),
        sqlite_where=sa.text("status = 'failed' AND finished_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_containers_stub_failed_finished", table_name="containers")
    op.drop_index("ix_containers_stub_failed_created", table_name="containers")
    op.drop_index("ix_containers_stub_live", table_name="containers")
    op.create_index(
        "ix_containers_stub_autoscaling",
        "containers",
        ["stub_id", "status", "finished_at", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'running', 'failed')"),
        sqlite_where=sa.text("status IN ('pending', 'running', 'failed')"),
    )
    op.drop_index("ix_autoscaling_targets_workspace", table_name="autoscaling_targets")
    op.drop_index("ix_autoscaling_targets_due", table_name="autoscaling_targets")
    op.drop_table("autoscaling_targets")
    op.drop_index("ix_stubs_reusable_identity", table_name="stubs")
