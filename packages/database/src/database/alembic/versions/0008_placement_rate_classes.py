"""Preserve placement rate identity on compute prices and billed usage."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_placement_rate_classes"
down_revision: str | None = "0007_provider_neutral_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("billing_compute_rates", "container_billing_shapes", "billing_ledger_segments")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "rate_class", sa.String(64), nullable=False, server_default=sa.text("'auto'")
            ),
        )
    _replace_rate_constraints(include_class=True)


def downgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        if connection.scalar(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE rate_class <> 'auto')")
        ):
            raise RuntimeError(
                "cannot remove placement rate classes while regional billing rows exist"
            )
    _replace_rate_constraints(include_class=False)
    for table in reversed(_TABLES):
        op.drop_column(table, "rate_class")


def _replace_rate_constraints(*, include_class: bool) -> None:
    table = "billing_compute_rates"
    identity = (
        ["billing_owner", "rate_class", "gpu_type"]
        if include_class
        else ["billing_owner", "gpu_type"]
    )
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        op.drop_constraint("ex_billing_compute_rates_window", table)
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint("uq_billing_compute_rates_start", type_="unique")
        batch.drop_index("ix_billing_compute_rates_lookup")
        batch.create_unique_constraint(
            "uq_billing_compute_rates_start", [*identity, "effective_at"]
        )
        batch.create_index("ix_billing_compute_rates_lookup", [*identity, "effective_at"])
    if not postgres:
        return
    equality = ", ".join(f"{column} WITH =" for column in identity)
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT ex_billing_compute_rates_window "
        f"EXCLUDE USING gist ({equality}, tstzrange(effective_at, valid_until, '[)') WITH &&)"
    )
