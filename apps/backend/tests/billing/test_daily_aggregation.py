"""Tests for daily usage finalization and billing."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from backend.database import Database
from backend.tasks.crons.usage import finalize_and_bill
from models.usage import DailyUsageStatus

from tests.billing.conftest import create_daily_record
from tests.fixtures.database import requires_db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.billing,
    requires_db,
]


@pytest.fixture
def mock_polar_service():
    """Mock Polar service."""
    with patch("backend.tasks.crons.usage.get_polar_service") as mock_get:
        mock_service = AsyncMock()
        mock_service.usage = AsyncMock()
        mock_service.usage.enabled = True
        mock_service.usage.send_daily_usage = AsyncMock(return_value=True)
        mock_get.return_value = mock_service
        yield mock_service


@pytest.fixture(autouse=True)
def mock_db_context(billing_db: Database, billing_db_session):
    """Mock get_db_context to use the test's database session."""

    @asynccontextmanager
    async def mock_context():
        try:
            yield billing_db
            await billing_db_session.flush()
            await billing_db_session.commit()
            billing_db_session.expire_all()
        except Exception:
            await billing_db_session.rollback()
            raise

    def get_mock_db_context():
        return mock_context()

    with patch(
        "backend.tasks.crons.usage.get_db_context",
        side_effect=get_mock_db_context,
    ):
        yield billing_db


class TestDailyBilling:
    """Test the finalize_and_bill flow."""

    async def test_bills_yesterdays_records(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing sends yesterday's records to Polar."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            cpu_core_seconds=3600.0,
            memory_gb_seconds=7200.0,
        )

        result = await finalize_and_bill()

        assert result["billed"] == 1
        assert result["failed"] == 0
        mock_polar_service.usage.send_daily_usage.assert_called_once()

        # Verify the call included expected data
        call_kwargs = mock_polar_service.usage.send_daily_usage.call_args.kwargs
        assert "record" in call_kwargs
        assert "idempotency_key" in call_kwargs
        assert call_kwargs["record"].cpu_core_seconds == 3600.0
        assert call_kwargs["record"].memory_gb_seconds == 7200.0

    async def test_marks_record_as_billed(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing marks the record as billed with correct status and ID."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            cpu_core_seconds=1800.0,
        )

        await finalize_and_bill()

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )

        assert len(records) == 1
        record = records[0]
        assert record.status == DailyUsageStatus.BILLED
        assert record.billing_id is not None
        assert len(record.billing_id) > 0
        assert record.billed_at is not None
        # Verify the record wasn't modified during billing
        assert record.cpu_core_seconds == 1800.0

    async def test_skips_already_billed_records(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify already billed records are not sent to Polar again."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        # Create and manually bill a record
        record = await billing_db.usage.get_or_create_daily_record(
            workspace_id=workspace_id,
            usage_date=yesterday,
        )
        await billing_db.usage.mark_as_billed(record.id, "existing-billing-id")

        result = await finalize_and_bill()

        assert result["billed"] == 0
        assert result["failed"] == 0
        mock_polar_service.usage.send_daily_usage.assert_not_called()

    async def test_only_bills_yesterday_not_today(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify only yesterday's records are billed, not today's."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        today = datetime.now(timezone.utc).date()

        # Create records for both days
        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )
        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=today,
        )

        result = await finalize_and_bill()

        # Should only bill yesterday
        assert result["billed"] == 1
        mock_polar_service.usage.send_daily_usage.assert_called_once()


class TestBillingWithIdempotencyKey:
    """Test idempotency key handling for exactly-once billing."""

    async def test_uses_record_id_as_idempotency_key(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify record ID is passed as idempotency key to Polar."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        record = await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        await finalize_and_bill()

        call_kwargs = mock_polar_service.usage.send_daily_usage.call_args.kwargs
        assert call_kwargs["idempotency_key"] == str(record.id)

    async def test_stores_idempotency_key_as_billing_id(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing_id stored matches the idempotency key."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        original_record = await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        await finalize_and_bill()

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )

        assert records[0].billing_id == str(original_record.id)


class TestBillingErrorHandling:
    """Test error handling in billing."""

    async def test_counts_polar_failure_as_failed(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify Polar send failure is counted correctly."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        mock_polar_service.usage.send_daily_usage.return_value = False

        result = await finalize_and_bill()

        assert result["billed"] == 0
        assert result["failed"] == 1

        # Record should not be marked as billed
        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )
        assert records[0].status != DailyUsageStatus.BILLED

    async def test_handles_polar_disabled(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify records are skipped when Polar is disabled."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        with patch("backend.tasks.crons.usage.get_polar_service") as mock_get:
            mock_service = AsyncMock()
            mock_service.usage = AsyncMock()
            mock_service.usage.enabled = False
            mock_get.return_value = mock_service

            result = await finalize_and_bill()

            # Records should be skipped when Polar is disabled
            assert result["billed"] == 0
            assert result["failed"] == 0
            assert result["skipped"] == 1

        # Verify record is NOT marked as billed (remains in collecting status)
        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )
        assert records[0].status != DailyUsageStatus.BILLED


class TestBillingRetryBehavior:
    """Test retry behavior with exponential backoff."""

    async def test_retries_on_transient_failure_then_succeeds(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing retries on failure and eventually succeeds."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        # Fail twice, succeed on third try
        mock_polar_service.usage.send_daily_usage.side_effect = [False, False, True]

        result = await finalize_and_bill()

        assert result["billed"] == 1
        assert result["failed"] == 0
        # Should have been called 3 times due to retries
        assert mock_polar_service.usage.send_daily_usage.call_count == 3

    async def test_fails_after_max_retries(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing fails after exhausting retries."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        # Always fail
        mock_polar_service.usage.send_daily_usage.return_value = False

        result = await finalize_and_bill()

        assert result["billed"] == 0
        assert result["failed"] == 1
        # Should have retried 3 times
        assert mock_polar_service.usage.send_daily_usage.call_count == 3


class TestBillingAttemptsTracking:
    """Test billing attempts counter and max attempts behavior."""

    async def test_increments_billing_attempts_on_failure(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing attempts counter is incremented on failure."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        mock_polar_service.usage.send_daily_usage.return_value = False

        await finalize_and_bill()

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )
        # Should have 2 attempts: 1 initial + 1 after retry failure
        assert records[0].billing_attempts >= 1
        assert records[0].last_billing_error is not None

    async def test_skips_records_exceeding_max_attempts(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify records are skipped when max billing attempts exceeded."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        # Create record with max attempts already exceeded
        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            billing_attempts=5,
        )

        result = await finalize_and_bill()

        assert result["billed"] == 0
        assert result["failed"] == 0
        assert result["skipped"] == 1
        mock_polar_service.usage.send_daily_usage.assert_not_called()

    async def test_records_last_billing_error(
        self,
        billing_db: Database,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify last billing error is recorded."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        error_msg = "Test error from Polar"
        mock_polar_service.usage.send_daily_usage.side_effect = Exception(error_msg)

        await finalize_and_bill()

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )
        assert error_msg in records[0].last_billing_error
        assert records[0].last_billing_attempt_at is not None


class TestBillingAuditLogging:
    """Test billing audit log creation."""

    async def test_logs_billing_started_event(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing_started audit event is created."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        await finalize_and_bill()

        # Check audit log has billing_started event
        from backend.database.tables.billing_audit import BillingAuditLogTable
        from sqlalchemy import select

        result = await billing_db_session.execute(
            select(BillingAuditLogTable)
            .where(BillingAuditLogTable.workspace_id == workspace_id)
            .where(BillingAuditLogTable.event_type == "billing_started")
        )
        events = result.scalars().all()
        assert len(events) >= 1

    async def test_logs_billing_completed_event(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing_completed audit event is created on success."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        await finalize_and_bill()

        from backend.database.tables.billing_audit import BillingAuditLogTable
        from sqlalchemy import select

        result = await billing_db_session.execute(
            select(BillingAuditLogTable)
            .where(BillingAuditLogTable.workspace_id == workspace_id)
            .where(BillingAuditLogTable.event_type == "billing_completed")
        )
        events = result.scalars().all()
        assert len(events) == 1
        assert events[0].details.get("billing_id") is not None

    async def test_logs_billing_failed_event(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing_failed audit event is created on failure."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
        )

        mock_polar_service.usage.send_daily_usage.return_value = False

        await finalize_and_bill()

        from backend.database.tables.billing_audit import BillingAuditLogTable
        from sqlalchemy import select

        result = await billing_db_session.execute(
            select(BillingAuditLogTable)
            .where(BillingAuditLogTable.workspace_id == workspace_id)
            .where(BillingAuditLogTable.event_type == "billing_failed")
        )
        events = result.scalars().all()
        assert len(events) >= 1
        assert "error" in events[0].details

    async def test_logs_billing_skipped_for_incomplete_intervals(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        mock_polar_service,
    ):
        """Verify billing_skipped audit event is created for incomplete intervals."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        # Create record with only 1 interval (not 96)
        record = await billing_db.usage.get_or_create_daily_record(
            workspace_id=workspace_id,
            usage_date=yesterday,
            expected_intervals=96,
        )
        await billing_db.usage.increment_usage(
            record_id=record.id,
            cpu_core_seconds=100.0,
            memory_gb_seconds=100.0,
            build_minutes=0.0,
        )

        await finalize_and_bill()

        from backend.database.tables.billing_audit import BillingAuditLogTable
        from sqlalchemy import select

        result = await billing_db_session.execute(
            select(BillingAuditLogTable)
            .where(BillingAuditLogTable.workspace_id == workspace_id)
            .where(BillingAuditLogTable.event_type == "billing_skipped")
        )
        events = result.scalars().all()
        assert len(events) == 1
        assert "Incomplete intervals" in events[0].details.get("reason", "")
