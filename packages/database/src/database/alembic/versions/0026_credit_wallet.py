"""Unify credit eligibility and remove runtime funding reservations."""

from alembic import op

revision = "0026_credit_wallet"
down_revision = "0025_storage_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE billing_funding_windows")
    op.execute("DROP TABLE billing_funding_allocations")
    op.execute("DROP TABLE billing_funding_holds")
    op.execute("ALTER TABLE billing_credit_lots DROP COLUMN scope")
    op.execute("""
        ALTER TABLE billing_credit_allocations
        ADD COLUMN id UUID NOT NULL DEFAULT gen_random_uuid(),
        DROP CONSTRAINT billing_credit_allocations_pkey,
        ADD PRIMARY KEY (id)
    """)
    op.execute("ALTER TABLE billing_credit_allocations ALTER COLUMN id DROP DEFAULT")
    op.execute("""
        CREATE INDEX ix_billing_credit_allocations_lot
        ON billing_credit_allocations (credit_lot_id)
    """)
    op.execute("""
        ALTER TABLE billing_credit_settlements
        ADD COLUMN waived_nanos BIGINT NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE billing_credit_settlements
        DROP CONSTRAINT ck_billing_credit_settlements_amounts,
        ADD CONSTRAINT ck_billing_credit_settlements_amounts CHECK (
            (settled_at IS NULL AND credited_nanos IS NULL AND payable_nanos IS NULL) OR
            (settled_at IS NOT NULL AND credited_nanos IS NOT NULL AND payable_nanos IS NOT NULL
             AND credited_nanos >= 0 AND payable_nanos >= 0 AND waived_nanos >= 0
             AND credited_nanos + payable_nanos + waived_nanos = gross_nanos)
        )
    """)


def downgrade() -> None:
    raise RuntimeError("credit wallet history cannot be restored to runtime reservations")
