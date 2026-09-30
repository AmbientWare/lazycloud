"""Recover deployment publication and exact container shutdowns after commit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_deployment_effects"
down_revision = "0033_scheduling_fairness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_preparations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("deployment", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deployment_preparations_expiry", "deployment_preparations", ["expires_at"])
    op.create_table(
        "deployment_effects",
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
        sa.Column(
            "deployment_id",
            sa.UUID(),
            sa.ForeignKey("deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("app_created", sa.Boolean(), nullable=False),
        sa.Column("source_stub_id", sa.UUID()),
        sa.Column("source_deleted", sa.Boolean(), nullable=False),
        sa.Column("app_revision", sa.BigInteger()),
        sa.CheckConstraint(
            "action IN ('created', 'started', 'stopped', 'deleted', 'scale.updated')",
            name="ck_deployment_effects_action",
        ),
    )
    op.create_index("ix_deployment_effects_due", "deployment_effects", ["retry_at", "id"])
    op.create_table(
        "deployment_shutdowns",
        sa.Column(
            "effect_id",
            sa.UUID(),
            sa.ForeignKey("deployment_effects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "container_id",
            sa.UUID(),
            sa.ForeignKey("containers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("worker_id", sa.String(240), nullable=False),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM deployment_effects) OR EXISTS (SELECT 1 FROM deployment_preparations) THEN RAISE EXCEPTION 'finish deployment effects before downgrading'; END IF; END $$"
    )
    op.drop_table("deployment_shutdowns")
    op.drop_table("deployment_effects")
    op.drop_table("deployment_preparations")
