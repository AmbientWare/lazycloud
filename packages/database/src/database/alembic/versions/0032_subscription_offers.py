"""Accept new subscription terms without changing existing paid agreements."""

from alembic import op

revision = "0032_subscription_offers"
down_revision = "0031_artifact_plan_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_billing_accounts_subscription_terms", "billing_accounts")
    op.create_check_constraint(
        "ck_billing_accounts_subscription_terms",
        "billing_accounts",
        "subscription_terms_version IS NULL OR "
        "(plan = 'free' AND subscription_terms_version IN ('free-v1', 'free-v2')) OR "
        "(plan = 'team' AND subscription_terms_version IN ('team-v1', 'team-v2', 'team-v3')) OR "
        "(plan = 'business' AND subscription_terms_version IN ('business-v1', 'business-v2'))",
    )
    op.drop_constraint("ck_billing_accounts_scheduled_terms", "billing_accounts")
    op.create_check_constraint(
        "ck_billing_accounts_scheduled_terms",
        "billing_accounts",
        "(scheduled_terms_version IS NULL AND scheduled_change_at IS NULL) OR "
        "(scheduled_terms_version IS NOT NULL AND scheduled_terms_version IN "
        "('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1', 'team-v3', 'business-v2') "
        "AND scheduled_change_at IS NOT NULL)",
    )
    op.drop_constraint("ck_billing_plan_change_intents_terms", "billing_plan_change_intents")
    op.create_check_constraint(
        "ck_billing_plan_change_intents_terms",
        "billing_plan_change_intents",
        "(target_plan = 'free' AND target_terms_version IN ('free-v1', 'free-v2')) OR "
        "(target_plan = 'team' AND target_terms_version IN ('team-v1', 'team-v2', 'team-v3')) OR "
        "(target_plan = 'business' AND target_terms_version IN ('business-v1', 'business-v2'))",
    )
    op.drop_constraint("ck_billing_allowance_periods_funded_terms", "billing_allowance_periods")
    op.create_check_constraint(
        "ck_billing_allowance_periods_funded_terms",
        "billing_allowance_periods",
        "funded_terms_version IS NULL OR funded_terms_version IN "
        "('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1', 'team-v3', 'business-v2')",
    )


def downgrade() -> None:
    raise RuntimeError("paid subscription terms must remain readable")
