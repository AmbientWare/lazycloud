"""Preserve credit sources and allocate them against the priced ledger."""

from alembic import op

revision = "0018_billing_credits"
down_revision = "0017_managed_compute_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE billing_meter_outbox ADD COLUMN usage_record_id UUID")
    op.execute("UPDATE billing_meter_outbox SET usage_record_id = identifier::uuid")
    op.execute("ALTER TABLE billing_meter_outbox ALTER COLUMN usage_record_id SET NOT NULL")
    op.execute(
        "CREATE INDEX ix_billing_meter_outbox_usage ON billing_meter_outbox (usage_record_id)"
    )
    op.execute("ALTER TABLE billing_meter_outbox ADD COLUMN metering_ended_at TIMESTAMPTZ")
    op.execute("""
        UPDATE billing_meter_outbox AS outbox SET metering_ended_at = (
            SELECT max(segment_ended_at) FROM billing_ledger_segments AS ledger
            WHERE ledger.usage_record_id = outbox.usage_record_id
        )
    """)
    op.execute("ALTER TABLE billing_meter_outbox ALTER COLUMN metering_ended_at SET NOT NULL")
    op.execute("""
        ALTER TABLE billing_meter_outbox ADD CONSTRAINT ck_billing_meter_outbox_window
        CHECK (metering_ended_at > occurred_at)
    """)
    op.execute("ALTER TABLE billing_allowance_periods ADD COLUMN credit_confirmed_at TIMESTAMPTZ")
    op.execute("""
        CREATE TABLE billing_credit_cutovers (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
            effective_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            blocked_reason VARCHAR(1024) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_billing_credit_cutovers_completion
                CHECK (completed_at IS NULL OR completed_at >= effective_at)
        )
    """)
    op.execute("""
        CREATE TABLE billing_credit_lots (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            source_id VARCHAR(255) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            scope VARCHAR(32) NOT NULL,
            amount_nanos BIGINT NOT NULL,
            effective_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_billing_credit_lots_source UNIQUE (user_id, source_id),
            CONSTRAINT ck_billing_credit_lots_amount CHECK (amount_nanos > 0),
            CONSTRAINT ck_billing_credit_lots_kind
                CHECK (kind IN ('purchased', 'subscription', 'trial')),
            CONSTRAINT ck_billing_credit_lots_scope CHECK (scope IN ('compute', 'all_metered')),
            CONSTRAINT ck_billing_credit_lots_expiry
                CHECK (expires_at IS NULL OR expires_at > effective_at),
            CONSTRAINT ck_billing_credit_lots_purchased
                CHECK (kind <> 'purchased' OR expires_at IS NULL)
        )
    """)
    op.execute("""
        CREATE INDEX ix_billing_credit_lots_account
        ON billing_credit_lots (user_id, effective_at, expires_at)
    """)
    op.execute("""
        CREATE TABLE billing_credit_allocations (
            credit_lot_id UUID REFERENCES billing_credit_lots(id) ON DELETE RESTRICT,
            ledger_segment_id UUID REFERENCES billing_ledger_segments(id) ON DELETE RESTRICT,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ NOT NULL,
            amount_nanos BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (credit_lot_id, ledger_segment_id, started_at),
            CONSTRAINT ck_billing_credit_allocations_amount CHECK (amount_nanos > 0),
            CONSTRAINT ck_billing_credit_allocations_window CHECK (ended_at > started_at)
        )
    """)
    op.execute("""
        CREATE INDEX ix_billing_credit_allocations_segment
        ON billing_credit_allocations (ledger_segment_id)
    """)
    op.execute("""
        CREATE TABLE billing_credit_settlements (
            usage_record_id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            gross_nanos BIGINT NOT NULL,
            credited_nanos BIGINT,
            payable_nanos BIGINT,
            settled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_billing_credit_settlements_gross CHECK (gross_nanos >= 0),
            CONSTRAINT ck_billing_credit_settlements_amounts CHECK (
                (settled_at IS NULL AND credited_nanos IS NULL AND payable_nanos IS NULL) OR
                (settled_at IS NOT NULL AND credited_nanos IS NOT NULL AND payable_nanos IS NOT NULL
                 AND credited_nanos >= 0 AND payable_nanos >= 0
                 AND credited_nanos + payable_nanos = gross_nanos)
            )
        )
    """)
    op.execute("""
        CREATE INDEX ix_billing_credit_settlements_pending
        ON billing_credit_settlements (user_id, settled_at)
    """)


def downgrade() -> None:
    raise RuntimeError("Credit sources and settled consumption cannot be discarded")
