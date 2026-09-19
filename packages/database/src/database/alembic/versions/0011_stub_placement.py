"""Drop the placement column from stubs.

A deployment keeps the placement it was deployed with on its own row. Every
other workload resolves its placement when a container is requested, so the
copy a stub carried since revision 0009 is no longer read.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_stub_placement"
down_revision = "0010_stub_config_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("stubs", "placement")


def downgrade() -> None:
    op.add_column(
        "stubs",
        sa.Column("placement", sa.String(120), nullable=False, server_default="platform"),
    )
    op.alter_column("stubs", "placement", server_default=None)
