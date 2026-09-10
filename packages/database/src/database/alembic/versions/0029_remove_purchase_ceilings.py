"""Remove obsolete node purchase ceilings from durable unit records."""

from alembic import op

revision = "0029_remove_purchase_ceilings"
down_revision = "0028_drop_credit_cutovers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE compute_units SET payload = payload - 'offer_max_hourly_cost_micros'")


def downgrade() -> None:
    raise RuntimeError("historical purchase ceilings cannot be reconstructed")
