"""Record resumable app deployment pruning."""

import sqlalchemy as sa
from alembic import op

revision = "0016_deployment_prunes"
down_revision = "0015_provider_commitments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_prunes",
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
            "app_id", sa.UUID(), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_deployment_prunes_pending",
        "deployment_prunes",
        ["retry_at"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_table(
        "deployment_prune_targets",
        sa.Column(
            "operation_id",
            sa.UUID(),
            sa.ForeignKey("deployment_prunes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "deployment_id",
            sa.UUID(),
            sa.ForeignKey("deployments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index(
        "ix_deployments_app_workload_live",
        "deployments",
        ["workspace_id", "app_id", "kind", "name", "version"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM deployment_prunes WHERE completed_at IS NULL) THEN RAISE EXCEPTION 'finish deployment pruning before downgrading'; END IF; END $$"
    )
    op.drop_table("deployment_prune_targets")
    op.drop_table("deployment_prunes")
    op.drop_index("ix_deployments_app_workload_live", table_name="deployments")
