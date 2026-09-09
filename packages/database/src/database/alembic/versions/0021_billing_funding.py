"""Reserve credit before capacity acquisition and fence paid runtime permits."""

from alembic import op

revision = "0021_billing_funding"
down_revision = "0020_credit_purchases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing_funding_holds (
            container_id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            worker_id VARCHAR(160) NOT NULL,
            revision INTEGER NOT NULL,
            billing_owner VARCHAR(40) NOT NULL,
            rate_class VARCHAR(64) NOT NULL,
            gpu_type VARCHAR(64) NOT NULL,
            cpu_millicores INTEGER NOT NULL,
            memory_mib INTEGER NOT NULL,
            gpu_count INTEGER NOT NULL,
            authorized_at TIMESTAMPTZ,
            valid_until TIMESTAMPTZ,
            metered_through TIMESTAMPTZ,
            terminal_at TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ,
            loss_resolved_at TIMESTAMPTZ,
            loss_machine_id UUID,
            loss_provider_instance_id UUID,
            loss_evidence_at TIMESTAMPTZ,
            loss_exposure_nanos BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_billing_funding_holds_revision CHECK (revision >= 1),
            CONSTRAINT ck_billing_funding_holds_resources
                CHECK (cpu_millicores >= 0 AND memory_mib >= 0 AND gpu_count >= 0),
            CONSTRAINT ck_billing_funding_holds_permit CHECK (
                (authorized_at IS NULL AND valid_until IS NULL) OR
                (authorized_at IS NOT NULL AND valid_until IS NOT NULL AND valid_until > authorized_at)
            ),
            CONSTRAINT ck_billing_funding_holds_terminal
                CHECK (terminal_at IS NULL OR (authorized_at IS NOT NULL AND terminal_at >= authorized_at)),
            CONSTRAINT ck_billing_funding_holds_loss_evidence CHECK (
                loss_resolved_at IS NULL OR (loss_machine_id IS NOT NULL
                AND loss_provider_instance_id IS NOT NULL AND loss_evidence_at IS NOT NULL
                AND loss_exposure_nanos IS NOT NULL AND loss_exposure_nanos >= 0)
            )
        )
    """)
    op.execute(
        "CREATE INDEX ix_billing_funding_holds_account ON billing_funding_holds (user_id, terminal_at)"
    )
    op.execute("""
        CREATE TABLE billing_funding_allocations (
            container_id UUID REFERENCES billing_funding_holds(container_id) ON DELETE RESTRICT,
            credit_lot_id UUID REFERENCES billing_credit_lots(id) ON DELETE RESTRICT,
            amount_nanos BIGINT NOT NULL,
            PRIMARY KEY (container_id, credit_lot_id),
            CONSTRAINT ck_billing_funding_allocations_amount CHECK (amount_nanos > 0)
        )
    """)
    op.execute(
        "CREATE INDEX ix_billing_funding_allocations_lot ON billing_funding_allocations (credit_lot_id)"
    )
    op.execute("""
        CREATE TABLE billing_funding_windows (
            container_id UUID REFERENCES billing_funding_holds(container_id) ON DELETE RESTRICT,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ NOT NULL,
            usage_record_ids JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (container_id, started_at),
            CONSTRAINT ck_billing_funding_windows_interval CHECK (ended_at > started_at)
        )
    """)


def downgrade() -> None:
    raise RuntimeError("Funded runtime permits and metering evidence cannot be discarded")
