"""Add the non-destructive machine draining state.

Revision ID: 0004_machine_draining
Revises: 0003_connection_capacity_limits
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_machine_draining"
down_revision: str | None = "0003_connection_capacity_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_compute_machine_enrollments_capacity_state"


def upgrade() -> None:
    with op.batch_alter_table("compute_machine_enrollments") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            _CONSTRAINT,
            "capacity_state IN ('available', 'draining', 'preempting', 'cordoned')",
        )


def downgrade() -> None:
    with op.batch_alter_table("compute_machine_enrollments") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            _CONSTRAINT,
            "capacity_state IN ('available', 'preempting', 'cordoned')",
        )
