"""Bind managed capacity to provider refs while retaining customer AWS connections."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_provider_neutral_capacity"
down_revision: str | None = "0004_machine_draining"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_compute_units_active_connection_gpu", table_name="compute_units")
    op.create_index(
        "ix_compute_units_active_provider_gpu",
        "compute_units",
        ["provider_ref", "worker_gpu_count", "desired_machines"],
        postgresql_where=sa.text("provider_ref <> '' AND desired_machines > 0"),
    )
    with op.batch_alter_table("compute_units") as batch:
        batch.create_check_constraint(
            "ck_compute_units_internal_provider_identity",
            "visibility <> 'internal' OR (provider_ref <> '' AND region <> '' "
            "AND offer_id <> '' AND capability_key <> '' AND capacity_mode = 'pooled' "
            "AND capacity_owner_kind = 'pooled_provider' AND capacity_owner_id = id "
            "AND (provider_connection_id IS NOT NULL "
            "OR COALESCE(CAST(payload->>'platform_fleet' AS BOOLEAN), false)))",
        )


def downgrade() -> None:
    with op.batch_alter_table("compute_units") as batch:
        batch.drop_constraint("ck_compute_units_internal_provider_identity", type_="check")
    op.drop_index("ix_compute_units_active_provider_gpu", table_name="compute_units")
    op.create_index(
        "ix_compute_units_active_connection_gpu",
        "compute_units",
        ["provider_connection_id", "worker_gpu_count", "desired_machines"],
        postgresql_where=sa.text("provider_connection_id IS NOT NULL AND desired_machines > 0"),
    )
