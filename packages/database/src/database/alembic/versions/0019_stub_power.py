"""Give stubs a power state: parked by a stop, woken by a start."""

import sqlalchemy as sa
from alembic import op

revision = "0019_stub_power"
down_revision = "0018_disk_stub"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stubs",
        sa.Column("parked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("stubs", sa.Column("woken_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("stubs", "woken_at")
    op.drop_column("stubs", "parked")
