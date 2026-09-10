"""Remove the monthly automatic reload payment limit."""

from alembic import op

revision = "0034_remove_reload_limit"
down_revision = "0033_aws_regional_networks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE billing_preferences
            DROP CONSTRAINT ck_billing_preferences_reload_amounts,
            DROP COLUMN reload_monthly_payment_limit_cents,
            ADD CONSTRAINT ck_billing_preferences_reload_amounts CHECK (
                reload_threshold_cents >= 0 AND reload_amount_cents > 0
            )
    """)


def downgrade() -> None:
    raise RuntimeError("removed monthly reload limits cannot be recovered by a schema downgrade")
