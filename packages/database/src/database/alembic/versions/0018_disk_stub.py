"""Record the stub whose pod last asked for each disk."""

import sqlalchemy as sa
from alembic import op

revision = "0018_disk_stub"
down_revision = "0017_disks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disks", sa.Column("last_stub_id", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("disks", "last_stub_id")
