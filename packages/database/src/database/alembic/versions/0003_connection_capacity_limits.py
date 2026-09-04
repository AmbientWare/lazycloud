"""Make the AWS connection the sole owner of pooled capacity ceilings.

Revision ID: 0003_connection_capacity_limits
Revises: 0002_scheduler_targets
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

revision: str = "0003_connection_capacity_limits"
down_revision: str | None = "0002_scheduler_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_INTEGER = TypeAdapter(int)


def upgrade() -> None:
    connection = op.get_bind()
    connections = sa.table(
        "aws_account_connections",
        sa.column("id", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    units = sa.table(
        "compute_units",
        sa.column("id", sa.String()),
        sa.column("payload", sa.JSON()),
    )

    for connection_id, raw_payload in connection.execute(
        sa.select(connections.c.id, connections.c.payload)
    ):
        payload = _JSON_OBJECT.validate_python(raw_payload)
        if payload.get("platform_fleet", False):
            continue
        raw_compute = payload.get("compute")
        if not isinstance(raw_compute, dict):
            continue
        compute = _JSON_OBJECT.validate_python(raw_compute)
        updated_compute = dict(compute)
        if updated_compute.get("max_cpu_instances") == 10:
            updated_compute["max_cpu_instances"] = None
        if updated_compute.get("max_gpu_instances") == 2:
            updated_compute["max_gpu_instances"] = None
        if updated_compute == compute:
            continue
        updated = dict(payload)
        updated["compute"] = updated_compute
        connection.execute(
            sa.update(connections).where(connections.c.id == connection_id).values(payload=updated)
        )

    for unit_id, raw_payload in connection.execute(sa.select(units.c.id, units.c.payload)):
        payload = _JSON_OBJECT.validate_python(raw_payload)
        if "workspace_machine_limit" not in payload:
            continue
        updated = dict(payload)
        updated.pop("workspace_machine_limit", None)
        connection.execute(sa.update(units).where(units.c.id == unit_id).values(payload=updated))

    with op.batch_alter_table("compute_units") as batch:
        batch.drop_column("workspace_machine_limit")
    op.create_index(
        "ix_compute_units_active_connection_gpu",
        "compute_units",
        ["provider_connection_id", "worker_gpu_count", "desired_machines"],
        unique=False,
        postgresql_where=sa.text("provider_connection_id IS NOT NULL AND desired_machines > 0"),
        sqlite_where=sa.text("provider_connection_id IS NOT NULL AND desired_machines > 0"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connections = sa.table(
        "aws_account_connections",
        sa.column("id", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    units = sa.table(
        "compute_units",
        sa.column("id", sa.String()),
        sa.column("max_machines", sa.BigInteger()),
        sa.column("payload", sa.JSON()),
    )

    op.drop_index("ix_compute_units_active_connection_gpu", table_name="compute_units")
    with op.batch_alter_table("compute_units") as batch:
        batch.add_column(
            sa.Column(
                "workspace_machine_limit",
                sa.Integer(),
                nullable=True,
            )
        )

    for connection_id, raw_payload in connection.execute(
        sa.select(connections.c.id, connections.c.payload)
    ):
        payload = _JSON_OBJECT.validate_python(raw_payload)
        raw_compute = payload.get("compute")
        if not isinstance(raw_compute, dict):
            continue
        compute = _JSON_OBJECT.validate_python(raw_compute)
        updated_compute = dict(compute)
        if updated_compute.get("max_cpu_instances") is None:
            updated_compute["max_cpu_instances"] = 10
        if updated_compute.get("max_gpu_instances") is None:
            updated_compute["max_gpu_instances"] = 2
        updated = dict(payload)
        updated["compute"] = updated_compute
        connection.execute(
            sa.update(connections).where(connections.c.id == connection_id).values(payload=updated)
        )

    for unit_id, maximum, raw_payload in connection.execute(
        sa.select(units.c.id, units.c.max_machines, units.c.payload)
    ):
        payload = _JSON_OBJECT.validate_python(raw_payload)
        restored_limit = _INTEGER.validate_python(maximum)
        updated: dict[str, JsonValue] = dict(payload)
        updated["workspace_machine_limit"] = restored_limit
        connection.execute(
            sa.update(units)
            .where(units.c.id == unit_id)
            .values(payload=updated, workspace_machine_limit=restored_limit)
        )

    with op.batch_alter_table("compute_units") as batch:
        batch.alter_column(
            "workspace_machine_limit",
            existing_type=sa.Integer(),
            nullable=False,
        )
