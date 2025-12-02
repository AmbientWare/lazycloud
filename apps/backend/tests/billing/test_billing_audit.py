"""Tests for billing audit log service."""

from datetime import datetime, timezone

import pytest
from backend.database import Database
from backend.database.billing_audit import BillingEventType

from tests.fixtures.database import requires_db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.billing,
    requires_db,
]


class TestBillingAuditService:
    """Test the BillingAuditService."""

    async def test_log_event_creates_audit_entry(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify log_event creates an audit log entry."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_event(
            event_type=BillingEventType.BILLING_STARTED,
            workspace_id=workspace_id,
            record_id="test-record-id",
            actor="test-actor",
            details={"test_key": "test_value"},
        )

        assert entry.event_type == BillingEventType.BILLING_STARTED.value
        assert entry.workspace_id == workspace_id
        assert entry.record_id == "test-record-id"
        assert entry.actor == "test-actor"
        assert entry.details["test_key"] == "test_value"
        assert entry.id is not None
        assert entry.created_at is not None

    async def test_log_collection_completed(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify log_collection_completed creates correct audit entry."""
        workspace_id = str(billing_workspace.id)
        interval_start = datetime.now(timezone.utc)

        entry = await billing_db.billing_audit.log_collection_completed(
            workspace_id=workspace_id,
            record_id="test-record-id",
            interval_start=interval_start,
            cpu_seconds=100.5,
            memory_seconds=200.5,
        )

        assert entry.event_type == BillingEventType.COLLECTION_COMPLETED.value
        assert entry.details["cpu_core_seconds"] == 100.5
        assert entry.details["memory_gb_seconds"] == 200.5
        assert entry.details["interval_start"] == interval_start.isoformat()

    async def test_log_billing_started(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify log_billing_started creates correct audit entry."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_billing_started(
            workspace_id=workspace_id,
            record_id="test-record-id",
            usage_date="2025-11-29",
            attempt=2,
        )

        assert entry.event_type == BillingEventType.BILLING_STARTED.value
        assert entry.details["usage_date"] == "2025-11-29"
        assert entry.details["attempt"] == 2

    async def test_log_billing_completed(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify log_billing_completed creates correct audit entry."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_billing_completed(
            workspace_id=workspace_id,
            record_id="test-record-id",
            billing_id="polar-billing-123",
            usage_date="2025-11-29",
        )

        assert entry.event_type == BillingEventType.BILLING_COMPLETED.value
        assert entry.details["billing_id"] == "polar-billing-123"
        assert entry.details["usage_date"] == "2025-11-29"

    async def test_log_billing_failed(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify log_billing_failed creates correct audit entry."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_billing_failed(
            workspace_id=workspace_id,
            record_id="test-record-id",
            error="Connection timeout to Polar API",
            attempt=3,
            usage_date="2025-11-29",
        )

        assert entry.event_type == BillingEventType.BILLING_FAILED.value
        assert "Connection timeout" in entry.details["error"]
        assert entry.details["attempt"] == 3
        assert entry.details["usage_date"] == "2025-11-29"

    async def test_log_billing_failed_truncates_long_error(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify long error messages are truncated."""
        workspace_id = str(billing_workspace.id)
        long_error = "x" * 1000

        entry = await billing_db.billing_audit.log_billing_failed(
            workspace_id=workspace_id,
            record_id="test-record-id",
            error=long_error,
            attempt=1,
            usage_date="2025-11-29",
        )

        assert len(entry.details["error"]) <= 500

    async def test_log_billing_skipped(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify log_billing_skipped creates correct audit entry."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_billing_skipped(
            workspace_id=workspace_id,
            record_id="test-record-id",
            reason="Incomplete intervals: 50/96",
            usage_date="2025-11-29",
        )

        assert entry.event_type == BillingEventType.BILLING_SKIPPED.value
        assert "Incomplete intervals" in entry.details["reason"]
        assert entry.details["usage_date"] == "2025-11-29"

    async def test_default_actor_is_system(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify actor defaults to 'system' when not provided."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_event(
            event_type=BillingEventType.BILLING_STARTED,
            workspace_id=workspace_id,
        )

        assert entry.actor == "system"

    async def test_details_defaults_to_empty_dict(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify details defaults to empty dict when not provided."""
        workspace_id = str(billing_workspace.id)

        entry = await billing_db.billing_audit.log_event(
            event_type=BillingEventType.BILLING_STARTED,
            workspace_id=workspace_id,
        )

        assert entry.details == {}
