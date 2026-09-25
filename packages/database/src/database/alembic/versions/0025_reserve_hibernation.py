"""Record whether a provider instance was launched able to hibernate."""

import sqlalchemy as sa
from alembic import op

revision = "0025_reserve_hibernation"
down_revision = "0024_task_attempt_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_provider_instances",
        sa.Column("hibernates", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("compute_provider_instances", "hibernates")
