"""Record prepaid payments and immutable corrections to purchased credit."""

from alembic import op

revision = "0020_credit_purchases"
down_revision = "0019_storage_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing_credit_adjustments (
            id UUID PRIMARY KEY,
            credit_lot_id UUID NOT NULL REFERENCES billing_credit_lots(id) ON DELETE RESTRICT,
            source_id VARCHAR(255) NOT NULL,
            amount_nanos BIGINT NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_billing_credit_adjustments_source UNIQUE (credit_lot_id, source_id),
            CONSTRAINT ck_billing_credit_adjustments_amount CHECK (amount_nanos <> 0)
        )
    """)
    op.execute("""
        CREATE TABLE credit_purchases (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            request_key UUID NOT NULL,
            kind VARCHAR(16) NOT NULL,
            amount_nanos BIGINT NOT NULL,
            status VARCHAR(32) NOT NULL,
            provider_customer_id VARCHAR(255) NOT NULL,
            provider_session_id VARCHAR(255),
            provider_payment_id VARCHAR(255),
            success_url VARCHAR(2048) NOT NULL,
            cancel_url VARCHAR(2048) NOT NULL,
            hosted_url VARCHAR(2048),
            session_expires_at TIMESTAMPTZ,
            creation_started_at TIMESTAMPTZ,
            credit_lot_id UUID REFERENCES billing_credit_lots(id) ON DELETE RESTRICT,
            funded_at TIMESTAMPTZ,
            reversed_nanos BIGINT NOT NULL,
            reversal_sequence INTEGER NOT NULL,
            last_error VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_credit_purchases_request UNIQUE (user_id, request_key),
            CONSTRAINT uq_credit_purchases_session UNIQUE (provider_session_id),
            CONSTRAINT uq_credit_purchases_payment UNIQUE (provider_payment_id),
            CONSTRAINT uq_credit_purchases_lot UNIQUE (credit_lot_id),
            CONSTRAINT ck_credit_purchases_amount CHECK (amount_nanos > 0),
            CONSTRAINT ck_credit_purchases_kind CHECK (kind IN ('manual', 'automatic')),
            CONSTRAINT ck_credit_purchases_status CHECK (
                status IN ('pending', 'action_required', 'succeeded', 'declined', 'cancelled')
            ),
            CONSTRAINT ck_credit_purchases_reversal CHECK (
                reversed_nanos >= 0 AND reversed_nanos <= amount_nanos
            )
        )
    """)
    op.execute("CREATE INDEX ix_credit_purchases_user_id ON credit_purchases (user_id)")
    op.execute("""
        CREATE INDEX ix_credit_purchases_reconciliation ON credit_purchases (status, updated_at)
    """)


def downgrade() -> None:
    raise RuntimeError("Prepaid payment history cannot be discarded")
