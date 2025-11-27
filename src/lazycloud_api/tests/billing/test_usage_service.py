"""Tests for UsageService aggregation logic."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lazycloud_api.database import Database
from lazycloud_api.services.cost_breakdown_service import CostBreakdownService
from lazycloud_api.services.depot_service import DepotService
from lazycloud_api.services.polar import PolarService
from lazycloud_api.services.usage_service import UsageService
from lazycloud_api.tests.billing.conftest import create_interval_records
from lazycloud_api.tests.fixtures.database import make_deployment, requires_db
from shared.models.billing import (
    SECONDS_PER_HOUR,
    UsageCollectionConfig,
    UsageRecordStatus,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.billing,
    requires_db,
]


@pytest.fixture
def usage_service() -> UsageService:
    """Create a UsageService instance with mock dependencies."""
    # Create minimal cost service (disabled Polar)
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
            # Flush and commit like the real get_db_context does
            await billing_db_session.flush()
            await billing_db_session.commit()
            # Expire all so subsequent queries see committed data
            billing_db_session.expire_all()
        except Exception:
            await billing_db_session.rollback()
            raise

    def get_mock_db_context():
        return mock_context()

    with patch(
        "lazycloud_api.services.usage_service.get_db_context",
        side_effect=get_mock_db_context,
    ):
        yield billing_db


class TestWorkspaceAggregation:
    """Test workspace-level usage aggregation."""

    async def test_aggregate_workspace_usage_for_range(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify workspace-level totals for date range."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        cpu_per_interval = 3600.0  # 1 core-hour worth
        memory_per_interval = 7200.0  # 2 GB-hours worth
        num_intervals = 24

        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=num_intervals,
            cpu_per_interval=cpu_per_interval,
            memory_per_interval=memory_per_interval,
        )

        # Flush records so aggregate_workspace_usage_for_date_range can see them
        await billing_db_session.flush()

        start_date = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = start_date + timedelta(days=1)

        # Note: aggregate_workspace_usage_for_date_range uses get_db_context internally
        # For this test, we verify the helper function works with seeded data
        metrics = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )

        expected_cpu_hours = (cpu_per_interval * num_intervals) / SECONDS_PER_HOUR
        expected_memory_hours = (memory_per_interval * num_intervals) / SECONDS_PER_HOUR

        assert metrics.cpu_core_hours == expected_cpu_hours
        assert metrics.memory_gb_hours == expected_memory_hours

    async def test_aggregate_workspace_with_return_records(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify aggregation returns records when requested."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        cpu_per_interval = 3600.0  # default from create_interval_records
        num_intervals = 24

        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=num_intervals,
            cpu_per_interval=cpu_per_interval,
        )

        # Flush records so aggregate_workspace_usage_for_date_range can see them
        await billing_db_session.flush()

        start_date = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = start_date + timedelta(days=1)

        result = await usage_service.aggregate_workspace_usage_for_date_range(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            return_records=True,
        )

        metrics, records = result

        # Verify correct number of records returned
        assert len(records) == num_intervals

        # Verify metrics match expected totals
        expected_cpu_hours = (cpu_per_interval * num_intervals) / SECONDS_PER_HOUR
        assert metrics.cpu_core_hours == expected_cpu_hours


class TestDeploymentAggregation:
    """Test deployment-level usage aggregation from records."""

    async def test_aggregate_deployment_usage_from_records(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        billing_deployment,
        usage_service: UsageService,
    ):
        """Verify deployment-level breakdown extraction."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        # Create a second deployment to test filtering
        other_deployment = await billing_db.compose_deployments.create(
            make_deployment(workspace_id)
        )
        other_deployment_id = str(other_deployment.id)

        # Create interval records with compute breakdowns
        records = []
        for i in range(3):
            start_time = day_start + timedelta(hours=i)
            end_time = start_time + timedelta(hours=1)

            record = await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=1000.0,
                memory_gb_seconds=2000.0,
                storage_gb_hours=0.0,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

            # Add compute breakdown for this deployment
            await billing_db.usage.upsert_compute_breakdown(
                usage_record_id=record.id,
                pod_name="web-0",
                cpu_core_seconds=500.0,
                memory_gb_seconds=1000.0,
                deployment_id=deployment_id,
                service_name="web",
            )

            # Also add breakdown for different deployment (should be excluded)
            await billing_db.usage.upsert_compute_breakdown(
                usage_record_id=record.id,
                pod_name="other-0",
                cpu_core_seconds=500.0,
                memory_gb_seconds=1000.0,
                deployment_id=other_deployment_id,
                service_name="other",
            )

            records.append(record)

        # Flush records so queries can see them
        await billing_db_session.flush()

        # Re-fetch records with breakdowns loaded
        usage_records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_start + timedelta(days=1),
            record_type=UsageCollectionConfig.get_record_type(),
        )

        metrics, services, volumes = (
            usage_service.aggregate_deployment_usage_from_records(
                usage_records=usage_records,
                deployment_id=deployment_id,
                build_minutes=0.0,
                public_endpoint_hours=0.0,
            )
        )

        # Should only include usage for the target deployment
        # 3 intervals * 500 core-seconds = 1500 core-seconds = 1500/3600 hours
        expected_cpu_hours = (500.0 * 3) / SECONDS_PER_HOUR
        expected_memory_hours = (1000.0 * 3) / SECONDS_PER_HOUR

        assert metrics.cpu_core_hours == pytest.approx(expected_cpu_hours, rel=0.01)
        assert metrics.memory_gb_hours == pytest.approx(expected_memory_hours, rel=0.01)

        # Should have service breakdown for "web"
        assert len(services) == 1
        assert services[0].service_name == "web"


class TestDailyByTimezone:
    """Test timezone-aware daily grouping."""

    async def test_aggregate_records_by_day_utc(
        self,
        billing_db: Database,
        billing_db_session,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify records grouped by calendar day in UTC."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        # Create records across midnight UTC
        for hour in range(24):
            start_time = day_start + timedelta(hours=hour)
            end_time = start_time + timedelta(hours=1)

            await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=100.0,
                memory_gb_seconds=100.0,
                storage_gb_hours=0.0,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

        # Flush records so queries can see them
        await billing_db_session.flush()

        # Fetch records
        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_start + timedelta(days=1),
            record_type=UsageCollectionConfig.get_record_type(),
        )

        # Test aggregation by day
        tz = ZoneInfo("UTC")
        daily_data = usage_service._aggregate_records_by_day(records, tz)

        # Should have exactly 1 day
        assert len(daily_data) == 1

        day_key = day_start.strftime("%Y-%m-%d")
        assert day_key in daily_data
        expected_hours = 24 * 100.0 / SECONDS_PER_HOUR
        assert daily_data[day_key].cpu_core_hours == pytest.approx(
            expected_hours, rel=1e-9
        )

    async def test_aggregate_records_by_day_with_timezone(
        self,
        billing_db: Database,
        billing_workspace,
        usage_service: UsageService,
    ):
        """Verify records grouped correctly in non-UTC timezone."""
        workspace_id = str(billing_workspace.id)

        # Create records at UTC midnight (which is 7pm EST previous day)
        utc_midnight = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)

        for hour in range(6):  # 6 hours: 00:00-06:00 UTC
            start_time = utc_midnight + timedelta(hours=hour)
            end_time = start_time + timedelta(hours=1)

            await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=100.0,
                memory_gb_seconds=100.0,
                storage_gb_hours=0.0,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=utc_midnight,
            end_date=utc_midnight + timedelta(hours=6),
            record_type=UsageCollectionConfig.get_record_type(),
        )

        # In America/New_York timezone:
        # 00:00 UTC = 19:00 EST (Jan 14)
        # 05:00 UTC = 00:00 EST (Jan 15)
        tz = ZoneInfo("America/New_York")
        daily_data = usage_service._aggregate_records_by_day(records, tz)

        # Should span 2 calendar days in EST
        assert len(daily_data) == 2


class TestAggregationEdgeCases:
    """Test edge cases in usage aggregation."""

    async def test_aggregate_empty_records(
        self,
        usage_service: UsageService,
    ):
        """Verify aggregation handles empty record list."""
        metrics, services, volumes = (
            usage_service.aggregate_deployment_usage_from_records(
                usage_records=[],
                deployment_id="test-deployment",
                build_minutes=5.0,
                public_endpoint_hours=10.0,
            )
        )

        # Should return zero metrics but include build/endpoint values
        assert metrics.cpu_core_hours == 0.0
        assert metrics.memory_gb_hours == 0.0
        assert metrics.build_minutes == 5.0
        assert metrics.public_endpoint_hours == 10.0
        assert len(services) == 0
        assert len(volumes) == 0

    async def test_aggregate_with_build_and_endpoints(
        self,
        billing_db: Database,
        billing_workspace,
        billing_deployment,
        usage_service: UsageService,
    ):
        """Verify build minutes and endpoint hours included in aggregation."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        record = await billing_db.usage.upsert_usage_record(
            workspace_id=workspace_id,
            collection_start=day_start,
            collection_end=day_start + timedelta(hours=1),
            cpu_core_seconds=1000.0,
            memory_gb_seconds=2000.0,
            storage_gb_hours=0.0,
            build_minutes=15.0,
            public_endpoint_hours=2.0,
            record_type=UsageCollectionConfig.get_record_type(),
            status=UsageRecordStatus.FINALIZED,
        )

        await billing_db.usage.upsert_compute_breakdown(
            usage_record_id=record.id,
            pod_name="web-0",
            cpu_core_seconds=1000.0,
            memory_gb_seconds=2000.0,
            deployment_id=deployment_id,
            service_name="web",
        )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_start + timedelta(hours=1),
            record_type=UsageCollectionConfig.get_record_type(),
        )

        metrics, _, _ = usage_service.aggregate_deployment_usage_from_records(
            usage_records=records,
            deployment_id=deployment_id,
            build_minutes=15.0,
            public_endpoint_hours=2.0,
        )

        assert metrics.build_minutes == 15.0
        assert metrics.public_endpoint_hours == 2.0
