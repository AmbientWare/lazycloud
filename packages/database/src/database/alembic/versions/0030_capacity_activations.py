"""Separate stop intent, observed outcome and each activation's timing."""

import sqlalchemy as sa
from alembic import op

revision = "0030_capacity_activations"
down_revision = "0029_fleet_demand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_units",
        sa.Column(
            "offer_architecture", sa.String(64), nullable=False, server_default=sa.text("''")
        ),
    )
    op.execute(
        "UPDATE compute_units u SET offer_architecture = COALESCE((SELECT min(i.architecture) FROM compute_provider_instances i WHERE i.pool_id = u.id AND i.architecture != '' HAVING count(DISTINCT i.architecture) = 1), '')"
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column("activation_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column("provider_running_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("compute_provider_instances", sa.Column("stop_mode", sa.Text(), nullable=True))
    op.add_column(
        "compute_provider_instances",
        sa.Column("sleep_outcome", sa.Text(), nullable=False, server_default=sa.text("'unknown'")),
    )
    op.create_check_constraint(
        "ck_compute_provider_instances_sleep_mode",
        "compute_provider_instances",
        "stop_mode IS NULL OR stop_mode IN ('stop', 'hibernate')",
    )
    op.create_check_constraint(
        "ck_compute_provider_instances_sleep_outcome",
        "compute_provider_instances",
        "sleep_outcome IN ('unknown', 'stopped', 'hibernated')",
    )
    op.create_table(
        "capacity_activations",
        sa.Column(
            "instance_record_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("compute_provider_instances.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sleep_outcome", sa.Text(), nullable=False),
        sa.Column("provider_running_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('provision', 'boot', 'resume')", name="ck_capacity_activation_kind"
        ),
        sa.CheckConstraint(
            "sleep_outcome IN ('unknown', 'stopped', 'hibernated')",
            name="ck_capacity_activation_sleep_outcome",
        ),
        sa.CheckConstraint(
            "failed_at IS NULL OR (ready_at IS NULL AND prepared_at IS NULL)",
            name="ck_capacity_activation_terminal",
        ),
    )
    op.create_index("ix_capacity_activations_requested", "capacity_activations", ["requested_at"])
    op.create_index(
        "uq_capacity_activation_pending",
        "capacity_activations",
        ["instance_record_id"],
        unique=True,
        postgresql_where=sa.text("ready_at IS NULL AND prepared_at IS NULL AND failed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_column("compute_units", "offer_architecture")
    op.drop_table("capacity_activations")
    op.drop_constraint("ck_compute_provider_instances_sleep_outcome", "compute_provider_instances")
    op.drop_constraint("ck_compute_provider_instances_sleep_mode", "compute_provider_instances")
    op.drop_column("compute_provider_instances", "sleep_outcome")
    op.drop_column("compute_provider_instances", "stop_mode")
    op.drop_column("compute_provider_instances", "provider_running_at")
    op.drop_column("compute_provider_instances", "activation_requested_at")
