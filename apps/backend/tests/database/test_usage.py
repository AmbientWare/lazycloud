"""Tests for UsageService database operations."""

from datetime import datetime, timedelta, timezone

import pytest
from backend.database import Database
from models.storage import STORAGE_CLASS_STANDARD
from models.usage import BreakdownType, DailyUsageStatus

from tests.fixtures.database import (
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestDailyRecordCRUD:
    """Test basic CRUD operations for daily usage records."""

    async def test_create_daily_record(self, db: Database):
        """Test creating a daily usage record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        assert record.id is not None
        assert record.workspace_id == workspace.id
        assert record.usage_date == today
        assert record.status == DailyUsageStatus.COLLECTING
        assert record.intervals_collected == 0

    async def test_get_or_create_returns_existing(self, db: Database):
        """Test that get_or_create returns existing record."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()
        first = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )
        second = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        assert first.id == second.id


class TestAtomicIncrement:
    """Test atomic increment operations."""

    async def test_atomic_increment_updates_totals(self, db: Database):
        """Test that atomic increment correctly updates totals."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        await db.usage.increment_usage(
            record_id=record.id,
            cpu_core_seconds=100.0,
            memory_gb_seconds=200.0,
            storage_gb_months=0.5,
            build_minutes=15.0,
        )

        records = await db.usage.get_workspace_daily_usage(
            workspace_id=workspace.id,
            start_date=today,
            end_date=today,
        )

        assert len(records) == 1
        updated = records[0]
        assert updated.cpu_core_seconds == 100.0
        assert updated.memory_gb_seconds == 200.0
        assert updated.storage_gb_months == 0.5
        assert updated.build_minutes == 15.0
        assert updated.intervals_collected == 1

    async def test_multiple_increments_accumulate(self, db: Database):
        """Test that multiple increments accumulate correctly."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        # First increment
        await db.usage.increment_usage(
            record_id=record.id,
            cpu_core_seconds=100.0,
            memory_gb_seconds=0.0,
            build_minutes=0.0,
        )

        # Second increment
        await db.usage.increment_usage(
            record_id=record.id,
            cpu_core_seconds=150.0,
            memory_gb_seconds=0.0,
            build_minutes=0.0,
        )

        records = await db.usage.get_workspace_daily_usage(
            workspace_id=workspace.id,
            start_date=today,
            end_date=today,
        )

        assert records[0].cpu_core_seconds == 250.0
        assert records[0].intervals_collected == 2


class TestIdempotency:
    """Test idempotency tracking."""

    async def test_mark_interval_collected(self, db: Database):
        """Test marking an interval as collected."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        interval_start = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )

        await db.usage.mark_interval_collected(workspace.id, interval_start)

        is_collected = await db.usage.is_interval_collected(
            workspace.id, interval_start
        )
        assert is_collected is True

    async def test_is_interval_collected_returns_false(self, db: Database):
        """Test that uncollected intervals return False."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        interval_start = datetime.now(timezone.utc)

        is_collected = await db.usage.is_interval_collected(
            workspace.id, interval_start
        )
        assert is_collected is False


class TestBillingStatus:
    """Test billing status operations."""

    async def test_mark_as_billed(self, db: Database):
        """Test marking a record as billed."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        await db.usage.mark_as_billed(record.id, "test-billing-id")

        records = await db.usage.get_workspace_daily_usage(
            workspace_id=workspace.id,
            start_date=today,
            end_date=today,
        )

        assert records[0].status == DailyUsageStatus.BILLED
        assert records[0].billing_id == "test-billing-id"
        assert records[0].billed_at is not None

    async def test_get_unbilled_for_date(self, db: Database):
        """Test getting unbilled records for a date."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=yesterday,
        )

        unbilled = await db.usage.get_unbilled_for_date(yesterday)

        assert len(unbilled) == 1
        assert unbilled[0].id == record.id

    async def test_billed_record_not_in_unbilled(self, db: Database):
        """Test that billed records are excluded from unbilled query."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=yesterday,
        )
        await db.usage.mark_as_billed(record.id, "billed")

        unbilled = await db.usage.get_unbilled_for_date(yesterday)

        assert len(unbilled) == 0


class TestBreakdownEvents:
    """Test breakdown event operations."""

    async def test_add_breakdown_event(self, db: Database):
        """Test adding a breakdown event."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        now = datetime.now(timezone.utc)
        interval_start = now.replace(minute=0, second=0, microsecond=0)
        interval_end = interval_start + timedelta(minutes=15)

        await db.usage.add_breakdown_event(
            workspace_id=workspace.id,
            interval_start=interval_start,
            interval_end=interval_end,
            breakdown_type=BreakdownType.COMPUTE,
            resource_name="web-0",
            service_name="web",
            cpu_core_seconds=100.0,
            memory_gb_seconds=200.0,
        )

        breakdown = await db.usage.get_service_breakdown(
            workspace_id=workspace.id,
            start_date=interval_start,
            end_date=interval_end + timedelta(seconds=1),
        )

        assert len(breakdown) == 1
        assert breakdown[0][0] == "web"
        assert breakdown[0][1] == 100.0  # CPU
        assert breakdown[0][2] == 200.0  # Memory

    async def test_get_volume_breakdown(self, db: Database):
        """Test getting volume breakdown."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        now = datetime.now(timezone.utc)
        interval_start = now.replace(minute=0, second=0, microsecond=0)
        interval_end = interval_start + timedelta(minutes=15)

        await db.usage.add_breakdown_event(
            workspace_id=workspace.id,
            interval_start=interval_start,
            interval_end=interval_end,
            breakdown_type=BreakdownType.STORAGE,
            resource_name="data-vol",
            storage_class=STORAGE_CLASS_STANDARD,
            gb_hours=10.0,
        )

        breakdown = await db.usage.get_volume_breakdown(
            workspace_id=workspace.id,
            start_date=interval_start,
            end_date=interval_end + timedelta(seconds=1),
        )

        assert len(breakdown) == 1
        assert breakdown[0][0] == "data-vol"
        assert breakdown[0][1] == STORAGE_CLASS_STANDARD
        assert breakdown[0][2] == 10.0


class TestMultipleWorkspaces:
    """Test usage tracking across multiple workspaces."""

    async def test_usage_isolated_per_workspace(self, db: Database):
        """Test that daily records are isolated per workspace."""
        user = await db.users.create(make_user())
        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(make_user_workspace(user.id, ws1.id))
        await db.user_workspaces.create(make_user_workspace(user.id, ws2.id))

        today = datetime.now(timezone.utc).date()

        # Create records for both workspaces
        rec1 = await db.usage.get_or_create_daily_record(ws1.id, today)
        rec2 = await db.usage.get_or_create_daily_record(ws2.id, today)

        await db.usage.increment_usage(rec1.id, 100.0, 0.0, 0.0)
        await db.usage.increment_usage(rec2.id, 200.0, 0.0, 0.0)

        ws1_records = await db.usage.get_workspace_daily_usage(ws1.id, today, today)
        ws2_records = await db.usage.get_workspace_daily_usage(ws2.id, today, today)

        assert ws1_records[0].cpu_core_seconds == 100.0
        assert ws2_records[0].cpu_core_seconds == 200.0
