"""Record the stub whose pod last asked for each disk, and give stubs SSH and role columns.

A stub's `ssh` flag and pod role move out of its configuration document into
columns, so listing the pods that serve SSH filters in SQL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_disk_stub"
down_revision = "0017_disks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disks", sa.Column("last_stub_id", sa.UUID(), nullable=True))
    op.add_column("stubs", sa.Column("ssh", sa.Boolean(), nullable=True))
    op.add_column("stubs", sa.Column("role", sa.String(16), nullable=True))
    op.execute(
        "UPDATE stubs SET ssh = (configuration->>'ssh')::boolean, "
        "configuration = configuration - 'ssh' WHERE configuration ? 'ssh'"
    )
    op.execute(
        "UPDATE stubs SET role = configuration->>'role', "
        "configuration = configuration - 'role' WHERE configuration ? 'role'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE stubs SET configuration = configuration || jsonb_build_object('ssh', ssh) "
        "WHERE ssh IS NOT NULL"
    )
    op.execute(
        "UPDATE stubs SET configuration = configuration || jsonb_build_object('role', role) "
        "WHERE role IS NOT NULL"
    )
    op.drop_column("stubs", "role")
    op.drop_column("stubs", "ssh")
    op.drop_column("disks", "last_stub_id")
