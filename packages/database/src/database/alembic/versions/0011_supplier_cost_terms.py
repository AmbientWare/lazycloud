"""Preserve supplier quotes without inventing historical cost breakdowns."""

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

revision = "0011_supplier_cost_terms"
down_revision = "0010_log_retention"
branch_labels = None
depends_on = None

_payload = TypeAdapter(dict[str, JsonValue])


def upgrade() -> None:
    connection = op.get_bind()
    instances = sa.table(
        "compute_provider_instances",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
        sa.column("hourly_cost_micros", sa.BigInteger()),
    )
    for row in connection.execute(sa.select(instances)).mappings():
        payload = _payload.validate_python(row["payload"])
        # A zero default never established that a supplier provided free capacity.
        hourly = row["hourly_cost_micros"]
        terms: dict[str, JsonValue] = {
            "historical_unallocated_hourly_micros": hourly if hourly > 0 else None,
        }
        for key in ("billing_minimum_seconds", "billing_quantum_seconds"):
            value = payload.pop(key, None)
            terms[key] = value if isinstance(value, int) and value > 0 else None
        payload.pop("hourly_cost_micros", None)
        payload["cost_terms"] = terms
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            storage = metadata.pop("storage_mb", None)
            payload["storage_mib"] = storage if isinstance(storage, int) and storage >= 0 else None
        connection.execute(
            instances.update().where(instances.c.id == row["id"]).values(payload=payload)
        )
    units = sa.table(
        "compute_units",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
    )
    for row in connection.execute(sa.select(units)).mappings():
        payload = _payload.validate_python(row["payload"])
        hourly = payload.pop("offer_hourly_cost_micros", None)
        payload["offer_cost_terms"] = (
            {"historical_unallocated_hourly_micros": hourly}
            if isinstance(hourly, int) and hourly > 0
            else None
        )
        connection.execute(units.update().where(units.c.id == row["id"]).values(payload=payload))
    op.drop_column("compute_provider_instances", "hourly_cost_micros")


def downgrade() -> None:
    raise RuntimeError("supplier cost breakdowns cannot be downgraded to ambiguous hourly totals")
