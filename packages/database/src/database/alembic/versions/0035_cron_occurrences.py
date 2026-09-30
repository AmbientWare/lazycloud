"""Identify scheduled occurrences without discarding existing run history."""

import sqlalchemy as sa
from alembic import op

revision = "0035_cron_occurrences"
down_revision = "0034_deployment_effects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cron_jobs",
        sa.Column(
            "revision", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
    )
    op.add_column("cron_job_runs", sa.Column("schedule_revision", sa.UUID(), nullable=True))
    op.add_column(
        "cron_job_runs", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_cron_job_runs_occurrence", "cron_job_runs", ["schedule_revision", "scheduled_at"]
    )
    op.create_check_constraint(
        "ck_cron_job_runs_occurrence",
        "cron_job_runs",
        "(schedule_revision IS NULL) = (scheduled_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cron_job_runs_occurrence", "cron_job_runs", type_="check")
    op.drop_constraint("uq_cron_job_runs_occurrence", "cron_job_runs", type_="unique")
    op.drop_column("cron_job_runs", "scheduled_at")
    op.drop_column("cron_job_runs", "schedule_revision")
    op.drop_column("cron_jobs", "revision")
