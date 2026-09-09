"""Retain deduplicated supplier storage request observations."""

from alembic import op

revision = "0019_storage_access"
down_revision = "0018_billing_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE storage_access_observations (
            id UUID PRIMARY KEY,
            provider VARCHAR(32) NOT NULL,
            bucket VARCHAR(255) NOT NULL,
            request_id VARCHAR(128) NOT NULL,
            operation VARCHAR(64) NOT NULL,
            request_class VARCHAR(16) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            status_code INTEGER NOT NULL,
            response_bytes BIGINT,
            source_region VARCHAR(64) NOT NULL,
            transfer_evidence VARCHAR(16) NOT NULL,
            workspace_id UUID REFERENCES workspaces(id) ON DELETE RESTRICT,
            CONSTRAINT ck_storage_access_bytes CHECK (response_bytes IS NULL OR response_bytes >= 0),
            CONSTRAINT ck_storage_access_status CHECK (status_code BETWEEN 100 AND 599),
            CONSTRAINT ck_storage_access_class CHECK (request_class IN ('read','write','delete','other')),
            CONSTRAINT ck_storage_access_transfer CHECK (transfer_evidence IN ('same_region','other_region','unknown'))
        )
    """)
    op.execute(
        "CREATE INDEX ix_storage_access_occurred ON storage_access_observations (occurred_at)"
    )
    op.execute(
        "CREATE INDEX ix_storage_access_workspace_occurred ON storage_access_observations (workspace_id, occurred_at)"
    )


def downgrade() -> None:
    raise RuntimeError("Storage access observations are durable financial evidence")
