"""Remove obsolete provider credit-grant and cutover metadata."""

from alembic import op

revision = "0028_drop_credit_cutovers"
down_revision = "0027_billing_preference_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("billing_credit_cutovers")
    op.drop_index("uq_billing_accounts_provider_credit_grant", table_name="billing_accounts")
    op.drop_column("billing_accounts", "provider_credit_grant_id")


def downgrade() -> None:
    raise RuntimeError("provider credit-grant and cutover metadata cannot be reconstructed")
