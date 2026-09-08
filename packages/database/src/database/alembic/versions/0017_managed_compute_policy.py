"""Remove customer provisioning overrides from cloud connections."""

from alembic import op

revision = "0017_managed_compute_policy"
down_revision = "0016_volume_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE aws_account_connections SET payload = payload - 'compute'")


def downgrade() -> None:
    raise RuntimeError("Removed customer provisioning overrides cannot be reconstructed")
