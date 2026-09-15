from alembic import op

revision = "0049_database_lookup_indexes"
down_revision = "0048_capacity_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_stubs_name_workspace", "stubs", ["name", "workspace_id"])
    op.create_index(
        "ix_compute_provider_instances_pool_status",
        "compute_provider_instances",
        ["pool_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_compute_provider_instances_pool_status", "compute_provider_instances")
    op.drop_index("ix_stubs_name_workspace", "stubs")
