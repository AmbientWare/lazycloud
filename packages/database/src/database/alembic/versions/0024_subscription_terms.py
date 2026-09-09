"""Preserve subscription terms independently of the current plan catalog."""

from alembic import op

revision = "0024_subscription_terms"
down_revision = "0023_automatic_reload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE billing_accounts
            DROP CONSTRAINT ck_billing_accounts_plan,
            ADD COLUMN subscription_terms_version VARCHAR(32),
            ADD COLUMN scheduled_terms_version VARCHAR(32),
            ADD COLUMN scheduled_change_at TIMESTAMPTZ,
            ADD CONSTRAINT ck_billing_accounts_plan CHECK (plan IN ('', 'free', 'team', 'business')),
            ADD CONSTRAINT ck_billing_accounts_subscription_terms CHECK (
                subscription_terms_version IS NULL OR
                (plan = 'free' AND subscription_terms_version IN ('free-v1', 'free-v2')) OR
                (plan = 'team' AND subscription_terms_version IN ('team-v1', 'team-v2')) OR
                (plan = 'business' AND subscription_terms_version = 'business-v1')
            ),
            ADD CONSTRAINT ck_billing_accounts_scheduled_terms CHECK (
                (scheduled_terms_version IS NULL AND scheduled_change_at IS NULL) OR
                (scheduled_terms_version IS NOT NULL AND
                 scheduled_terms_version IN ('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1')
                 AND scheduled_change_at IS NOT NULL)
            )
    """)
    op.execute("""
        ALTER TABLE billing_allowance_periods
            ADD COLUMN funded_terms_version VARCHAR(32),
            ADD CONSTRAINT ck_billing_allowance_periods_funded_terms CHECK (
                funded_terms_version IS NULL OR funded_terms_version IN
                ('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1')
            )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM billing_plan_change_intents WHERE target_plan NOT IN ('free', 'team')
            ) THEN
                RAISE EXCEPTION 'Existing plan change target has no reviewed legacy subscription terms';
            END IF;
        END $$
    """)
    op.execute(
        "ALTER TABLE billing_plan_change_intents ADD COLUMN target_terms_version VARCHAR(32)"
    )
    op.execute("""
        UPDATE billing_plan_change_intents
        SET target_terms_version = CASE target_plan WHEN 'free' THEN 'free-v1' ELSE 'team-v1' END
    """)
    op.execute("""
        ALTER TABLE billing_plan_change_intents
            ALTER COLUMN target_terms_version SET NOT NULL,
            ADD CONSTRAINT ck_billing_plan_change_intents_terms CHECK (
                (target_plan = 'free' AND target_terms_version IN ('free-v1', 'free-v2')) OR
                (target_plan = 'team' AND target_terms_version IN ('team-v1', 'team-v2')) OR
                (target_plan = 'business' AND target_terms_version = 'business-v1')
            )
    """)


def downgrade() -> None:
    raise RuntimeError("historical subscription terms cannot be discarded by a schema downgrade")
