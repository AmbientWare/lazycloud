"""Save automatic reload terms and retain failed payment pauses."""

from alembic import op

revision = "0023_automatic_reload"
down_revision = "0022_billing_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE billing_preferences
            ADD COLUMN reload_enabled BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN reload_threshold_cents INTEGER NOT NULL DEFAULT 1000,
            ADD COLUMN reload_amount_cents INTEGER NOT NULL DEFAULT 2000,
            ADD COLUMN reload_monthly_payment_limit_cents BIGINT,
            ADD COLUMN reload_paused_purchase_id UUID REFERENCES credit_purchases(id) ON DELETE RESTRICT,
            ADD COLUMN reload_pause_reason VARCHAR(32) NOT NULL DEFAULT '',
            ADD COLUMN reload_checked_at TIMESTAMPTZ,
            ADD COLUMN reload_resumed_at TIMESTAMPTZ,
            ADD CONSTRAINT ck_billing_preferences_reload_amounts CHECK (
                reload_threshold_cents >= 0 AND reload_amount_cents > 0 AND
                (reload_monthly_payment_limit_cents IS NULL OR reload_monthly_payment_limit_cents >= 0)
            ),
            ADD CONSTRAINT ck_billing_preferences_reload_pause CHECK (
                (reload_paused_purchase_id IS NULL AND reload_pause_reason = '') OR
                (reload_paused_purchase_id IS NOT NULL AND reload_pause_reason IN ('declined', 'action_required'))
            )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_credit_purchases_pending_automatic
        ON credit_purchases (user_id)
        WHERE kind = 'automatic' AND status IN ('pending', 'action_required')
    """)
    op.execute("""
        CREATE INDEX ix_billing_preferences_reload_due
        ON billing_preferences (reload_checked_at, user_id)
        WHERE reload_enabled AND reload_paused_purchase_id IS NULL
    """)


def downgrade() -> None:
    raise RuntimeError("automatic reload policy cannot be discarded by a schema downgrade")
