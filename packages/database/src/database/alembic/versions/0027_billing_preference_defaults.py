"""Keep automatic reload defaults in the billing preferences contract."""

from alembic import op

revision = "0027_billing_preference_defaults"
down_revision = "0026_credit_wallet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("billing_preferences", "reload_amount_cents", server_default=None)


def downgrade() -> None:
    raise RuntimeError("billing preference defaults belong to the application")
