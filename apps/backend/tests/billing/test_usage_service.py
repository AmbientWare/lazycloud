"""Tests for UsageService aggregation logic."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from backend.database import Database
from backend.services.cost_breakdown_service import CostBreakdownService
from backend.services.depot_service import DepotService
from backend.services.polar import PolarService
from backend.services.usage_service import UsageService
from models.billing import SECONDS_PER_HOUR
from models.storage import STORAGE_CLASS_EBS
from sqlalchemy.ext.asyncio import AsyncSession

from tests.billing.conftest import create_breakdown_events, create_daily_record
from tests.fixtures.database import requires_db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.billing,
    requires_db,
]


@pytest.fixture
def usage_service() -> UsageService:
    """Create a UsageService instance with mock dependencies."""
    polar_service = PolarService(access_token="", is_sandbox=True)
    cost_service = CostBreakdownService(polar_service=polar_service)
    depot_service = DepotService(api_token="", org_id="")
    return UsageService(cost_service=cost_service, depot_service=depot_service)


@pytest.fixture(autouse=True)
def mock_db_context(billing_db: Database, billing_db_session: AsyncSession):
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
        "backend.services.usage_service.get_db_context",
        side_effect=get_mock_db_context,
    ):
        yield billing_db


class TestWorkspaceAggregation:
    """Test workspace-level usage aggregation from daily records."""

    async def test_aggregate_workspace_usage_for_range(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify workspace totals are correctly converted to hours."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        # 3600 seconds = 1 hour, 7200 seconds = 2 hours
        cpu_seconds = 3600.0
        memory_seconds = 7200.0

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            cpu_core_seconds=cpu_seconds,
            memory_gb_seconds=memory_seconds,
        )

        await billing_db_session.flush()

        start_date = datetime.combine(yesterday, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_date = start_date + timedelta(days=1)

        metrics = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )

        # Verify conversion from seconds to hours
        assert metrics.cpu_core_hours == 1.0  # 3600 / 3600
        assert metrics.memory_gb_hours == 2.0  # 7200 / 3600

    async def test_aggregate_workspace_multiple_days(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify aggregation sums across multiple daily records."""
        workspace_id = str(billing_workspace.id)
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)

        # Create records for two days with different values
        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            cpu_core_seconds=1000.0,
            standard_gb_hours=5.0,
        )
        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=today,
            cpu_core_seconds=2000.0,
            standard_gb_hours=10.0,
        )

        await billing_db_session.flush()

        start_date = datetime.combine(yesterday, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_date = datetime.combine(today, datetime.max.time()).replace(
            tzinfo=timezone.utc
        )

        metrics = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )

        # Verify sum: (1000 + 2000) / 3600 hours
        expected_cpu_hours = 3000.0 / SECONDS_PER_HOUR
        assert metrics.cpu_core_hours == pytest.approx(expected_cpu_hours, rel=0.001)
        # Storage is already in hours, so just sum
        assert metrics.standard_gb_hours == 15.0


class TestDeploymentBreakdown:
    """Test deployment-level usage breakdown from events."""

    async def test_get_deployment_breakdown_metrics(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        billing_deployment,
        usage_service: UsageService,
    ):
        """Verify deployment breakdown correctly aggregates event metrics."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        today = datetime.now(timezone.utc).date()
        interval_start = datetime.combine(today, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        interval_end = interval_start + timedelta(minutes=15)

        # Create 2 pods with 100 CPU each = 200 total
        await create_breakdown_events(
            db=billing_db,
            workspace_id=workspace_id,
            interval_start=interval_start,
            interval_end=interval_end,
            deployment_id=deployment_id,
            num_pods=2,
            cpu_per_pod=100.0,
            memory_per_pod=200.0,
            num_pvcs=1,
            gb_hours_per_pvc=10.0,
        )

        await billing_db_session.flush()

        metrics, services, volumes = await usage_service.get_deployment_breakdown(
            workspace_id=workspace_id,
            deployment_id=deployment_id,
            start_date=interval_start,
            end_date=interval_end + timedelta(hours=1),
        )

        # Verify metrics: 2 pods * 100 = 200 core-seconds
        expected_cpu_hours = 200.0 / SECONDS_PER_HOUR
        assert metrics.cpu_core_hours == pytest.approx(expected_cpu_hours, rel=0.001)

        expected_memory_hours = 400.0 / SECONDS_PER_HOUR
        assert metrics.memory_gb_hours == pytest.approx(
            expected_memory_hours, rel=0.001
        )

        assert metrics.standard_gb_hours == 10.0

    async def test_get_deployment_breakdown_service_list(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        billing_deployment,
        usage_service: UsageService,
    ):
        """Verify service breakdown contains correct per-service values."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        today = datetime.now(timezone.utc).date()
        interval_start = datetime.combine(today, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        interval_end = interval_start + timedelta(minutes=15)

        await create_breakdown_events(
            db=billing_db,
            workspace_id=workspace_id,
            interval_start=interval_start,
            interval_end=interval_end,
            deployment_id=deployment_id,
            num_pods=3,
            cpu_per_pod=50.0,
        )

        await billing_db_session.flush()

        metrics, services, volumes = await usage_service.get_deployment_breakdown(
            workspace_id=workspace_id,
            deployment_id=deployment_id,
            start_date=interval_start,
            end_date=interval_end + timedelta(hours=1),
        )

        # Should have 3 services (service-0, service-1, service-2)
        assert len(services) == 3
        for svc in services:
            assert svc.cpu_core_seconds == 50.0
            assert svc.service_name.startswith("service-")

    async def test_get_deployment_breakdown_volume_list(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        billing_deployment,
        usage_service: UsageService,
    ):
        """Verify volume breakdown contains correct per-volume values."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        today = datetime.now(timezone.utc).date()
        interval_start = datetime.combine(today, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        interval_end = interval_start + timedelta(minutes=15)

        await create_breakdown_events(
            db=billing_db,
            workspace_id=workspace_id,
            interval_start=interval_start,
            interval_end=interval_end,
            deployment_id=deployment_id,
            num_pods=0,
            num_pvcs=2,
            gb_hours_per_pvc=25.0,
        )

        await billing_db_session.flush()

        metrics, services, volumes = await usage_service.get_deployment_breakdown(
            workspace_id=workspace_id,
            deployment_id=deployment_id,
            start_date=interval_start,
            end_date=interval_end + timedelta(hours=1),
        )

        assert len(volumes) == 2
        for vol in volumes:
            assert vol.gb_hours == 25.0
            assert vol.volume_name.startswith("pvc-")
            assert vol.storage_class == STORAGE_CLASS_EBS


class TestDailyByTimezone:
    """Test timezone-aware daily grouping."""

    async def test_aggregate_daily_records_by_day_utc(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify records grouped correctly by calendar day in UTC."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            cpu_core_seconds=100.0,
        )

        await billing_db_session.flush()

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=yesterday,
            end_date=yesterday,
        )

        tz = ZoneInfo("UTC")
        daily_data = usage_service._aggregate_daily_records_by_day(records, tz)

        assert len(daily_data) == 1
        day_key = str(yesterday)
        assert day_key in daily_data
        # Verify the actual value was preserved
        assert daily_data[day_key].cpu_core_hours == 100.0 / SECONDS_PER_HOUR


class TestAggregationEdgeCases:
    """Test edge cases in usage aggregation."""

    async def test_aggregate_empty_range_returns_zeros(
        self,
        billing_db: Database,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify empty date range returns zero metrics, not errors."""
        workspace_id = str(billing_workspace.id)
        future_date = datetime.now(timezone.utc) + timedelta(days=30)

        metrics = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace_id,
            start_date=future_date,
            end_date=future_date + timedelta(days=1),
        )

        assert metrics.cpu_core_hours == 0.0
        assert metrics.memory_gb_hours == 0.0
        assert metrics.standard_gb_hours == 0.0
        assert metrics.shared_gb_hours == 0.0
        assert metrics.build_minutes == 0.0
        assert metrics.public_endpoint_hours == 0.0

    async def test_aggregate_with_all_metrics(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify all metric types are aggregated correctly."""
        workspace_id = str(billing_workspace.id)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        await create_daily_record(
            db=billing_db,
            workspace_id=workspace_id,
            usage_date=yesterday,
            cpu_core_seconds=1000.0,
            memory_gb_seconds=2000.0,
            standard_gb_hours=10.0,
            shared_gb_hours=5.0,
            build_minutes=15.0,
            public_endpoint_hours=2.0,
        )

        await billing_db_session.flush()

        start_date = datetime.combine(yesterday, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_date = start_date + timedelta(days=1)

        metrics = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )

        # Seconds are converted to hours
        assert metrics.cpu_core_hours == pytest.approx(1000.0 / 3600.0, rel=0.001)
        assert metrics.memory_gb_hours == pytest.approx(2000.0 / 3600.0, rel=0.001)
        # GB-hours and other metrics are stored directly
        assert metrics.standard_gb_hours == 10.0
        assert metrics.shared_gb_hours == 5.0
        assert metrics.build_minutes == 15.0
        assert metrics.public_endpoint_hours == 2.0

    async def test_nonexistent_workspace_returns_zeros(
        self,
        billing_db: Database,
        usage_service: UsageService,
    ):
        """Verify querying unknown workspace returns empty metrics."""
        fake_workspace_id = "00000000-0000-0000-0000-000000000000"
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        start_date = datetime.combine(yesterday, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_date = start_date + timedelta(days=1)

        metrics = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=fake_workspace_id,
            start_date=start_date,
            end_date=end_date,
        )

        assert metrics.cpu_core_hours == 0.0
        assert metrics.memory_gb_hours == 0.0
