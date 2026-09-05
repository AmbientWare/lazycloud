"""Persist build dispatch intent with the build record."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pydantic import TypeAdapter

revision: str = "0005_image_build_dispatch"
down_revision: str | None = "0004_machine_draining"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "image_builds",
        sa.Column("execution_cleanup_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_image_builds_cleanup_due",
        "image_builds",
        ["execution_cleanup_after"],
        postgresql_where=sa.text("execution_cleanup_after IS NOT NULL"),
    )
    op.create_index(
        "ix_image_builds_active_updated",
        "image_builds",
        ["updated_at"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_table(
        "image_build_logs",
        sa.Column(
            "build_id",
            sa.Uuid(),
            sa.ForeignKey("image_builds.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_table(
        "image_build_requests",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "build_id",
            sa.Uuid(),
            sa.ForeignKey("image_builds.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    builds = sa.table(
        "image_builds",
        sa.column("id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    logs = sa.table(
        "image_build_logs",
        sa.column("build_id", sa.Uuid()),
        sa.column("sequence", sa.BigInteger()),
        sa.column("message", sa.Text()),
    )
    connection = op.get_bind()
    messages_adapter = TypeAdapter(list[str])
    for build_id, messages in connection.execute(
        sa.select(builds.c.id, builds.c.payload["logs"]).where(
            builds.c.status.not_in(("pending", "running"))
        )
    ).yield_per(256):
        entries = messages_adapter.validate_python(messages or [])
        if entries:
            connection.execute(
                logs.insert(),
                [
                    {"build_id": build_id, "sequence": sequence, "message": message}
                    for sequence, message in enumerate(entries, start=1)
                ],
            )
    op.create_index("ix_image_build_requests_build_id", "image_build_requests", ["build_id"])
    op.add_column("image_builds", sa.Column("dispatch_payload", sa.Text(), nullable=True))
    op.add_column(
        "image_builds", sa.Column("dispatch_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("image_builds", sa.Column("dispatch_claim_id", sa.String(64), nullable=True))
    op.add_column(
        "image_builds", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_image_builds_dispatch_due",
        "image_builds",
        ["dispatch_after"],
        postgresql_where=sa.text("dispatch_payload IS NOT NULL AND dispatched_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_image_builds_cleanup_due", table_name="image_builds")
    op.drop_index("ix_image_builds_active_updated", table_name="image_builds")
    op.drop_column("image_builds", "execution_cleanup_after")
    op.drop_table("image_build_requests")
    op.drop_table("image_build_logs")
    op.drop_index("ix_image_builds_dispatch_due", table_name="image_builds")
    for column in ("dispatched_at", "dispatch_claim_id", "dispatch_after", "dispatch_payload"):
        op.drop_column("image_builds", column)
