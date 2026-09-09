"""Save account usage budgets independently of payment funding."""

from alembic import op

revision = "0022_billing_preferences"
down_revision = "0021_billing_funding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing_preferences (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
            monthly_usage_limit_nanos BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_billing_preferences_usage_limit
                CHECK (monthly_usage_limit_nanos IS NULL OR monthly_usage_limit_nanos >= 0)
        )
    """)


def downgrade() -> None:
    raise RuntimeError("billing preferences cannot be discarded by a schema downgrade")
