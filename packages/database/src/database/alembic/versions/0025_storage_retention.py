"""Record unfunded storage grace periods without removing billing history."""

from alembic import op

revision = "0025_storage_retention"
down_revision = "0024_subscription_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE storage_retention_periods (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            notification_message_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_storage_retention_periods_window
                CHECK (ended_at IS NULL OR ended_at >= started_at)
        )
    """)
    op.execute("""
        CREATE INDEX ix_storage_retention_periods_account
        ON storage_retention_periods (user_id, started_at)
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_storage_retention_periods_open
        ON storage_retention_periods (user_id) WHERE ended_at IS NULL
    """)


def downgrade() -> None:
    raise RuntimeError("storage retention history cannot be discarded by a schema downgrade")
