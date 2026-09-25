"""Record when a stream authorized a stopped reserve's resume."""

import sqlalchemy as sa
from alembic import op

revision = "0026_reserve_resume_authorized"
down_revision = "0025_reserve_hibernation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compute_provider_instances",
        sa.Column("resume_authorized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("compute_provider_instances", "resume_authorized_at")
