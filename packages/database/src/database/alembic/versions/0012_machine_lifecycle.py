"""One durable lifecycle on the machine row.

A machine's life was described by the provider row's bootstrap phase, the
enrollment's readiness, the machine's resource status and a service state
derived per request. The machine row now carries the phase itself, with the
message and failure reason beside it and the time it entered the phase, and
the provider row's bootstrap columns go away.

Existing rows are backfilled from the status the machine had and its
enrollment: a running machine with a ready enrollment is `ready`; a stopped
one whose authority was revoked is `terminating`, and any other stopped one
(offline, or waiting to be joined again) is `joining` so it can rejoin.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_machine_lifecycle"
down_revision = "0011_stub_placement"
branch_labels = None
depends_on = None

_LIFECYCLES = (
    "'requested', 'provisioning', 'booting', 'joining', 'ready', "
    "'draining', 'terminating', 'deleted', 'failed'"
)


def upgrade() -> None:
    op.add_column("machines", sa.Column("lifecycle", sa.String(32), nullable=True))
    op.add_column(
        "machines",
        sa.Column("lifecycle_message", sa.String(512), nullable=False, server_default=""),
    )
    op.add_column("machines", sa.Column("lifecycle_failure", sa.String(64), nullable=True))
    op.add_column(
        "machines",
        sa.Column("lifecycle_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE machines AS m
        SET lifecycle = CASE
                WHEN m.status = 'deleted' THEN 'deleted'
                WHEN m.status = 'failed' THEN 'failed'
                WHEN m.status = 'stopped' AND EXISTS (
                    SELECT 1 FROM compute_machine_enrollments AS e
                    WHERE e.machine_id = m.id
                      AND e.status IN ('revoked', 'deleted')
                ) THEN 'terminating'
                WHEN m.status = 'stopped' THEN 'joining'
                WHEN m.status = 'running' AND EXISTS (
                    SELECT 1 FROM compute_machine_enrollments AS e
                    WHERE e.machine_id = m.id
                      AND e.status = 'active'
                      AND e.readiness_phase = 'ready'
                ) THEN 'ready'
                ELSE 'joining'
            END,
            lifecycle_failure = CASE
                WHEN m.status = 'failed' THEN 'unknown'
                ELSE NULL
            END,
            lifecycle_at = m.updated_at
        """
    )
    op.alter_column("machines", "lifecycle", nullable=False)
    op.alter_column("machines", "lifecycle_at", nullable=False)
    op.alter_column("machines", "lifecycle_message", server_default=None)
    op.create_check_constraint(
        "ck_machines_lifecycle",
        "machines",
        f"lifecycle IN ({_LIFECYCLES})",
    )
    op.create_index(
        "ix_machines_placement_lifecycle",
        "machines",
        ["placement", "lifecycle"],
    )
    for column in (
        "bootstrap_phase",
        "bootstrap_failure_reason",
        "bootstrap_failure_detail",
        "bootstrap_observed_at",
        "bootstrap_phase_started_at",
    ):
        op.drop_column("compute_provider_instances", column)


def downgrade() -> None:
    op.add_column(
        "compute_provider_instances",
        sa.Column("bootstrap_phase_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column(
            "bootstrap_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column("bootstrap_failure_detail", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column("bootstrap_failure_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column("bootstrap_phase", sa.Text(), nullable=False, server_default="requested"),
    )
    for column in ("bootstrap_phase", "bootstrap_failure_detail", "bootstrap_observed_at"):
        op.alter_column("compute_provider_instances", column, server_default=None)
    op.drop_index("ix_machines_placement_lifecycle", table_name="machines")
    op.drop_constraint("ck_machines_lifecycle", "machines", type_="check")
    for column in ("lifecycle_at", "lifecycle_failure", "lifecycle_message", "lifecycle"):
        op.drop_column("machines", column)
