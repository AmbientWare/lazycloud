"""Name joined machines per account and replace the capacity label with a placement.

A placement is an identity: `platform`, `connection:<connection id>` or
`machine:<machine id>`. It replaces the free-form `pool` label everywhere the
label was stamped, and the connected account no longer carries a label of its
own because its placement is derived from its id. A deployment records the
machine it named; a stub carries its placement as its own column rather than
inside its runtime config; the workspace policy no longer holds a default.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_named_machines"
down_revision = "0008_workspace_connection"
branch_labels = None
depends_on = None

_PLATFORM = "platform"
_LEGACY_PLATFORM_LABELS = "('lazycloud', 'default', 'aws', '')"
_UNIT_STAMPED_TABLES = (
    "machines",
    "compute_join_credentials",
    "compute_machine_enrollments",
)
_INDEXED_FLEET_TABLES = ("machines", "workers", "agents")


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

    # Units first: every other stamped row copies its unit's placement.
    op.add_column("compute_units", sa.Column("placement", sa.String(120), nullable=True))
    op.execute(
        "UPDATE compute_units SET placement = 'connection:' || provider_connection_id "
        "WHERE provider_connection_id IS NOT NULL"
    )
    op.execute(
        f"UPDATE compute_units SET placement = '{_PLATFORM}' "
        f"WHERE placement IS NULL AND pool IN {_LEGACY_PLATFORM_LABELS}"
    )
    # A joined machine's unit was labelled with the machine's own name; it is the
    # unit's only machine, so the placement names that machine.
    op.execute(
        """
        UPDATE compute_units u SET placement = 'machine:' || m.id
        FROM machines m
        WHERE u.placement IS NULL
          AND u.capacity_owner_kind = 'workspace_agent'
          AND m.capacity_owner_id = u.capacity_owner_id::text
          AND m.pool = u.pool
          AND (
            SELECT count(*) FROM machines m2 WHERE m2.capacity_owner_id = u.capacity_owner_id::text
          ) = 1
        """
    )
    op.execute(f"UPDATE compute_units SET placement = '{_PLATFORM}' WHERE placement IS NULL")
    op.alter_column("compute_units", "placement", nullable=False)
    op.drop_index("ix_compute_units_workspace_pool", table_name="compute_units")
    op.create_index(
        "ix_compute_units_workspace_placement", "compute_units", ["workspace_id", "placement"]
    )
    op.drop_column("compute_units", "default_eligible")

    for table in _UNIT_STAMPED_TABLES:
        op.add_column(table, sa.Column("placement", sa.String(120), nullable=True))
        op.execute(
            f"UPDATE {table} t SET placement = u.placement FROM compute_units u "
            "WHERE u.capacity_owner_id::text = t.capacity_owner_id::text"
        )
        op.execute(f"UPDATE {table} SET placement = '{_PLATFORM}' WHERE placement IS NULL")
        op.alter_column(table, "placement", nullable=False)
    op.add_column("workers", sa.Column("placement", sa.String(120), nullable=True))
    op.execute(
        "UPDATE workers w SET placement = m.placement FROM machines m WHERE m.id = w.machine_id"
    )
    op.execute(f"UPDATE workers SET placement = '{_PLATFORM}' WHERE placement IS NULL")
    op.alter_column("workers", "placement", nullable=False)
    op.add_column(
        "agents",
        sa.Column("placement", sa.String(120), nullable=False, server_default=_PLATFORM),
    )
    op.alter_column("agents", "placement", server_default=None)

    # Workload rows named a label; resolve it the way the workload would be
    # resolved today: the workspace's connection, else the unit that carried
    # the same label in the workspace, else the platform.
    op.alter_column("stubs", "runtime_pool_selector", new_column_name="pool")
    op.add_column("stubs", sa.Column("placement", sa.String(120), nullable=True))
    op.add_column("deployments", sa.Column("placement", sa.String(120), nullable=True))
    op.add_column("containers", sa.Column("scheduling_placement", sa.String(120), nullable=True))
    # A container without a recorded request keeps a null placement.
    for table, target, label, scope in (
        ("stubs", "placement", "pool", "TRUE"),
        ("deployments", "placement", "pool", "TRUE"),
        (
            "containers",
            "scheduling_placement",
            "scheduling_pool_selector",
            "scheduling_requested_at IS NOT NULL",
        ),
    ):
        op.execute(
            f"""
            UPDATE {table} t SET {target} = 'connection:' || w.connection_id
            FROM workspaces w
            WHERE w.id = t.workspace_id AND w.connection_id IS NOT NULL AND {scope}
              AND (t.{label} IS NULL OR t.{label} IN {_LEGACY_PLATFORM_LABELS})
            """
        )
        op.execute(
            f"UPDATE {table} SET {target} = '{_PLATFORM}' WHERE {target} IS NULL AND {scope} "
            f"AND ({label} IS NULL OR {label} IN {_LEGACY_PLATFORM_LABELS})"
        )
        op.execute(
            f"""
            UPDATE {table} t SET {target} = u.placement
            FROM compute_units u
            WHERE t.{target} IS NULL AND {scope}
              AND u.workspace_id = t.workspace_id AND u.pool = t.{label}
            """
        )
        op.execute(
            f"UPDATE {table} SET {target} = '{_PLATFORM}' WHERE {target} IS NULL AND {scope}"
        )
        if table != "containers":
            op.alter_column(table, target, nullable=False)
        op.drop_column(table, label)

    for table in _INDEXED_FLEET_TABLES:
        op.drop_index(f"ix_{table}_pool_status", table_name=table)
        op.create_index(f"ix_{table}_placement_status", table, ["placement", "status"])
    for table in ("compute_units", *_UNIT_STAMPED_TABLES, "workers", "agents"):
        op.drop_column(table, "pool")

    op.drop_column("aws_account_connections", "pool")
    op.drop_constraint("ck_aws_account_connections_drain", "aws_account_connections", type_="check")
    op.alter_column(
        "aws_account_connections", "drain_total_pools", new_column_name="drain_total_units"
    )
    op.alter_column(
        "aws_account_connections",
        "drain_remaining_pools",
        new_column_name="drain_remaining_units",
    )
    op.create_check_constraint(
        "ck_aws_account_connections_drain",
        "aws_account_connections",
        "drain_remaining_units >= 0 AND drain_total_units >= drain_remaining_units",
    )


def downgrade() -> None:
    op.drop_constraint("ck_aws_account_connections_drain", "aws_account_connections", type_="check")
    op.alter_column(
        "aws_account_connections", "drain_total_units", new_column_name="drain_total_pools"
    )
    op.alter_column(
        "aws_account_connections",
        "drain_remaining_units",
        new_column_name="drain_remaining_pools",
    )
    op.create_check_constraint(
        "ck_aws_account_connections_drain",
        "aws_account_connections",
        "drain_remaining_pools >= 0 AND drain_total_pools >= drain_remaining_pools",
    )
    op.add_column(
        "aws_account_connections",
        sa.Column("pool", sa.String(240), nullable=False, server_default="aws"),
    )
    op.alter_column("aws_account_connections", "pool", server_default=None)

    for table in _INDEXED_FLEET_TABLES:
        op.drop_index(f"ix_{table}_placement_status", table_name=table)
    for table in ("compute_units", *_UNIT_STAMPED_TABLES, "workers", "agents"):
        op.add_column(table, sa.Column("pool", sa.String(240), nullable=True))
        op.execute(
            f"UPDATE {table} SET pool = CASE WHEN placement = '{_PLATFORM}' THEN 'lazycloud' "
            "WHEN placement LIKE 'connection:%' THEN 'aws' ELSE placement END"
        )
        op.alter_column(table, "pool", nullable=False)
        op.drop_column(table, "placement")
    for table in _INDEXED_FLEET_TABLES:
        op.create_index(f"ix_{table}_pool_status", table, ["pool", "status"])
    op.add_column(
        "compute_units",
        sa.Column("default_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("compute_units", "default_eligible", server_default=None)
    op.drop_index("ix_compute_units_workspace_placement", table_name="compute_units")
    op.create_index("ix_compute_units_workspace_pool", "compute_units", ["workspace_id", "pool"])

    op.add_column(
        "containers",
        sa.Column("scheduling_pool_selector", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("containers", "scheduling_pool_selector", server_default=None)
    op.drop_column("containers", "scheduling_placement")
    op.drop_column("deployments", "placement")
    op.add_column(
        "deployments", sa.Column("pool", sa.String(240), nullable=False, server_default="lazycloud")
    )
    op.alter_column("deployments", "pool", server_default=None)
    op.drop_column("stubs", "placement")
    op.add_column("stubs", sa.Column("runtime_pool_selector", sa.String(240), nullable=True))

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
