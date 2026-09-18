"""Name joined machines per account and list the workspaces each one serves.

Pools leave the public surface. A deployment records the machine it named and
the capacity label that resolved to; a stub carries that label as its own
column rather than inside its runtime config; the workspace policy no longer
holds a default pool; the placeholder `default` label on fleet rows becomes
the platform label.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_named_machines"
down_revision = "0008_workspace_connection"
branch_labels = None
depends_on = None

_FLEET_TABLES = ("machines", "workers", "agents")


def upgrade() -> None:
    op.add_column("machines", sa.Column("name", sa.String(63), nullable=True))
    op.add_column(
        "machines",
        sa.Column(
            "owner_user_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("users.id", name="fk_machines_owner_user_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_machines_name_owner", "machines", "name IS NULL OR owner_user_id IS NOT NULL"
    )
    op.create_index(
        "uq_machines_owner_name",
        "machines",
        ["owner_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("name IS NOT NULL AND status <> 'deleted'"),
    )
    op.create_table(
        "machine_workspaces",
        sa.Column(
            "machine_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("machines.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_machine_workspaces_workspace", "machine_workspaces", ["workspace_id"])
    op.add_column(
        "deployments",
        sa.Column("machine", sa.String(63), nullable=False, server_default=""),
    )
    op.drop_column("workspace_compute_policies", "default_pool")
    op.alter_column("stubs", "runtime_pool_selector", new_column_name="pool")
    op.execute("UPDATE stubs SET pool = '' WHERE pool IS NULL")
    op.alter_column("stubs", "pool", nullable=False, server_default="")
    for table in _FLEET_TABLES:
        op.execute(f"UPDATE {table} SET pool = 'lazycloud' WHERE pool = 'default'")


def downgrade() -> None:
    op.alter_column("stubs", "pool", nullable=True, server_default=None)
    op.alter_column("stubs", "pool", new_column_name="runtime_pool_selector")
    op.add_column(
        "workspace_compute_policies",
        sa.Column("default_pool", sa.String(240), nullable=False, server_default="lazycloud"),
    )
    op.drop_column("deployments", "machine")
    op.drop_index("ix_machine_workspaces_workspace", table_name="machine_workspaces")
    op.drop_table("machine_workspaces")
    op.drop_index("uq_machines_owner_name", table_name="machines")
    op.drop_constraint("ck_machines_name_owner", "machines", type_="check")
    op.drop_constraint("fk_machines_owner_user_id", "machines", type_="foreignkey")
    op.drop_column("machines", "owner_user_id")
    op.drop_column("machines", "name")
