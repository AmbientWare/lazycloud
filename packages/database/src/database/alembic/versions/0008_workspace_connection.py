"""Pin each workspace to the connected account it lives in; drop stored bucket keys."""

import sqlalchemy as sa
from alembic import op

revision = "0008_workspace_connection"
down_revision = "0007_function_result_display"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The stored key pair has no successor. A workspace that still holds one
    # would keep its bucket coordinates with nothing able to reach them, so the
    # operator detaches or deletes those workspaces before this runs.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM workspaces WHERE storage_credential_key IS NOT NULL) THEN
                RAISE EXCEPTION
                    'workspaces still hold externally attached storage credentials; '
                    'delete those workspaces before upgrading';
            END IF;
        END $$
        """
    )
    op.drop_constraint("ck_workspaces_storage_credentials", "workspaces", type_="check")
    op.drop_constraint("ck_workspaces_platform_namespace", "workspaces", type_="check")
    op.drop_column("workspaces", "storage_access_key_ciphertext")
    op.drop_column("workspaces", "storage_secret_key_ciphertext")
    op.drop_column("workspaces", "storage_credential_key")
    op.drop_column("workspaces", "storage_force_path_style")
    op.execute("UPDATE workspaces SET storage_endpoint_url = '' WHERE storage_endpoint_url IS NULL")
    op.execute("UPDATE workspaces SET storage_region = '' WHERE storage_region IS NULL")
    op.alter_column("workspaces", "storage_endpoint_url", nullable=False)
    op.alter_column("workspaces", "storage_region", nullable=False)
    op.add_column(
        "workspaces",
        sa.Column(
            "connection_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey(
                "aws_account_connections.id",
                name="fk_workspaces_connection_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
    )
    op.create_index("ix_workspaces_connection_id", "workspaces", ["connection_id"])
    op.create_check_constraint(
        "ck_workspaces_platform_namespace",
        "workspaces",
        "kind <> 'platform' OR (status = 'active' AND primary_token_id IS NULL "
        "AND concurrency_limit_id IS NULL AND storage_bucket IS NULL "
        "AND connection_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_platform_namespace", "workspaces", type_="check")
    op.drop_index("ix_workspaces_connection_id", table_name="workspaces")
    op.drop_constraint("fk_workspaces_connection_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "connection_id")
    op.alter_column("workspaces", "storage_endpoint_url", nullable=True)
    op.alter_column("workspaces", "storage_region", nullable=True)
    op.add_column("workspaces", sa.Column("storage_access_key_ciphertext", sa.Text()))
    op.add_column("workspaces", sa.Column("storage_secret_key_ciphertext", sa.Text()))
    op.add_column("workspaces", sa.Column("storage_credential_key", sa.Text()))
    op.add_column("workspaces", sa.Column("storage_force_path_style", sa.Boolean()))
    op.create_check_constraint(
        "ck_workspaces_platform_namespace",
        "workspaces",
        "kind <> 'platform' OR (status = 'active' AND primary_token_id IS NULL "
        "AND concurrency_limit_id IS NULL AND storage_bucket IS NULL "
        "AND storage_credential_key IS NULL)",
    )
    op.create_check_constraint(
        "ck_workspaces_storage_credentials",
        "workspaces",
        "(storage_credential_key IS NULL AND storage_access_key_ciphertext IS NULL "
        "AND storage_secret_key_ciphertext IS NULL) OR "
        "(storage_credential_key IS NOT NULL AND storage_access_key_ciphertext IS NOT NULL "
        "AND storage_secret_key_ciphertext IS NOT NULL)",
    )
