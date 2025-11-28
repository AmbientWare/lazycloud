"""Tests for daily usage aggregation from interval records."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from backend.database import Database
from backend.prefect_app.usage_collector import collect_workspace_daily_usage
from models.billing import (
    UsageCollectionConfig,
    UsageRecordStatus,
    UsageRecordType,
)

from tests.billing.conftest import create_interval_records
from tests.fixtures.database import requires_db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.billing,
    requires_db,
]


@pytest.fixture(autouse=True)
def mock_db_context(billing_db: Database, billing_db_session):
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
        "backend.prefect_app.usage_collector.get_db_context",
        side_effect=get_mock_db_context,
    ):
        yield billing_db


class TestDailyAggregationFromIntervals:
    """Test daily record aggregation from interval records."""

    async def test_daily_aggregation_from_intervals(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify daily record aggregates from interval records."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        # Create 24 interval records for yesterday
        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=24,
            cpu_per_interval=100.0,  # 100 core-seconds per hour
            memory_per_interval=200.0,  # 200 GB-seconds per hour
        )

        # Define day boundaries
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        # Run daily aggregation
        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True
        assert "usage_record_id" in result

        # Verify daily record exists
        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]
        assert daily_record.record_type == UsageRecordType.DAILY.value


class TestDailyAggregationTotals:
    """Test that daily totals correctly sum interval records."""

    async def test_daily_aggregation_cpu_totals(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify CPU core-seconds summed correctly across intervals."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        cpu_per_interval = 150.0
        num_intervals = 24

        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=num_intervals,
            cpu_per_interval=cpu_per_interval,
            memory_per_interval=0.0,
        )

        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        expected_total_cpu = cpu_per_interval * num_intervals
        assert daily_record.cpu_core_seconds == expected_total_cpu

    async def test_daily_aggregation_memory_totals(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify memory GB-seconds summed correctly across intervals."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        memory_per_interval = 500.0
        num_intervals = 24

        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=num_intervals,
            cpu_per_interval=0.0,
            memory_per_interval=memory_per_interval,
        )

        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        expected_total_memory = memory_per_interval * num_intervals
        assert daily_record.memory_gb_seconds == expected_total_memory

    async def test_daily_aggregation_storage_totals(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify storage (EBS+EFS) summed correctly across intervals."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        standard_per_interval = 10.0
        shared_per_interval = 5.0
        num_intervals = 24
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        # Create intervals with storage values
        for i in range(num_intervals):
            start_time = day_start + timedelta(hours=i)
            end_time = start_time + timedelta(hours=1)

            await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=0.0,
                memory_gb_seconds=0.0,
                storage_gb_hours=standard_per_interval + shared_per_interval,
                standard_gb_hours=standard_per_interval,
                shared_gb_hours=shared_per_interval,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        expected_standard = standard_per_interval * num_intervals
        expected_shared = shared_per_interval * num_intervals

        assert daily_record.standard_gb_hours == expected_standard
        assert daily_record.shared_gb_hours == expected_shared


class TestDailyAggregationStatus:
    """Test status handling in daily aggregation."""

    async def test_daily_aggregation_sets_finalized_status(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify DAILY record gets FINALIZED status."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=24,
        )

        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]
        assert daily_record.status == UsageRecordStatus.FINALIZED


class TestDailyAggregationBreakdowns:
    """Test breakdown aggregation in daily records."""

    async def test_daily_aggregation_compute_breakdowns_summed(
        self,
        billing_db: Database,
        billing_workspace,
        billing_deployment,
    ):
        """Verify per-pod breakdowns are summed correctly across intervals."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        num_intervals = 3
        cpu_per_interval = 100.0
        memory_per_interval = 200.0

        # Create interval records with compute breakdowns
        for i in range(num_intervals):
            start_time = day_start + timedelta(hours=i)
            end_time = start_time + timedelta(hours=1)

            record = await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=cpu_per_interval,
                memory_gb_seconds=memory_per_interval,
                storage_gb_hours=0.0,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

            await billing_db.usage.upsert_compute_breakdown(
                usage_record_id=record.id,
                pod_name="web-0",
                cpu_core_seconds=cpu_per_interval,
                memory_gb_seconds=memory_per_interval,
                deployment_id=deployment_id,
                service_name="web",
            )

        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        # Must have compute breakdown
        assert len(daily_record.compute_breakdowns) == 1

        # Find and verify the breakdown
        web_breakdown = next(
            (b for b in daily_record.compute_breakdowns if b.pod_name == "web-0"),
            None,
        )
        assert web_breakdown is not None, "Missing breakdown for web-0 pod"

        # Values should be summed: num_intervals * per_interval
        expected_cpu = cpu_per_interval * num_intervals
        expected_memory = memory_per_interval * num_intervals
        assert web_breakdown.cpu_core_seconds == expected_cpu
        assert web_breakdown.memory_gb_seconds == expected_memory

    async def test_daily_aggregation_multiple_pods_aggregated_separately(
        self,
        billing_db: Database,
        billing_workspace,
        billing_deployment,
    ):
        """Verify each pod gets its own breakdown in daily aggregation."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        pod_cpu_values = {"web-0": 100.0, "web-1": 200.0, "worker-0": 150.0}
        num_intervals = 2

        for i in range(num_intervals):
            start_time = day_start + timedelta(hours=i)
            end_time = start_time + timedelta(hours=1)

            total_cpu = sum(pod_cpu_values.values())
            record = await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=total_cpu,
                memory_gb_seconds=0.0,
                storage_gb_hours=0.0,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

            for pod_name, cpu_value in pod_cpu_values.items():
                await billing_db.usage.upsert_compute_breakdown(
                    usage_record_id=record.id,
                    pod_name=pod_name,
                    cpu_core_seconds=cpu_value,
                    memory_gb_seconds=0.0,
                    deployment_id=deployment_id,
                    service_name=pod_name.split("-")[0],
                )

        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        # Should have 3 separate breakdowns
        assert len(daily_record.compute_breakdowns) == 3

        # Verify each pod's breakdown is summed correctly
        for pod_name, cpu_per_interval in pod_cpu_values.items():
            breakdown = next(
                (b for b in daily_record.compute_breakdowns if b.pod_name == pod_name),
                None,
            )
            assert breakdown is not None, f"Missing breakdown for {pod_name}"
            expected_cpu = cpu_per_interval * num_intervals
            assert breakdown.cpu_core_seconds == expected_cpu

    async def test_daily_aggregation_storage_breakdowns_summed(
        self,
        billing_db: Database,
        billing_workspace,
        billing_deployment,
    ):
        """Verify storage breakdowns are summed correctly by PVC."""
        workspace_id = str(billing_workspace.id)
        deployment_id = str(billing_deployment.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        gb_hours_per_interval = 5.0
        num_intervals = 4

        for i in range(num_intervals):
            start_time = day_start + timedelta(hours=i)
            end_time = start_time + timedelta(hours=1)

            record = await billing_db.usage.upsert_usage_record(
                workspace_id=workspace_id,
                collection_start=start_time,
                collection_end=end_time,
                cpu_core_seconds=0.0,
                memory_gb_seconds=0.0,
                storage_gb_hours=gb_hours_per_interval,
                standard_gb_hours=gb_hours_per_interval,
                shared_gb_hours=0.0,
                record_type=UsageCollectionConfig.get_record_type(),
                status=UsageRecordStatus.FINALIZED,
            )

            await billing_db.usage.upsert_storage_breakdown(
                usage_record_id=record.id,
                pvc_name="data-volume",
                storage_class="ebs-sc",
                gb_hours=gb_hours_per_interval,
                deployment_id=deployment_id,
            )

        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        # Should have storage breakdown
        assert len(daily_record.storage_breakdowns) == 1

        storage_breakdown = daily_record.storage_breakdowns[0]
        assert storage_breakdown.pvc_name == "data-volume"
        assert storage_breakdown.storage_class == "ebs-sc"
        expected_gb_hours = gb_hours_per_interval * num_intervals
        assert storage_breakdown.gb_hours == expected_gb_hours


class TestDailyAggregationEdgeCases:
    """Test edge cases in daily aggregation."""

    async def test_daily_aggregation_no_intervals_fails(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify failure when no interval records exist."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        # Don't create any interval records
        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        # Should fail gracefully
        assert result["success"] is False
        assert "error" in result

    async def test_daily_aggregation_partial_intervals(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify aggregation works with partial day of intervals."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        # Only 12 intervals (half day)
        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=12,
            cpu_per_interval=100.0,
        )

        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        result = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
        daily_record = records[0]

        # Should only sum the 12 intervals: 100 * 12 = 1200
        assert daily_record.cpu_core_seconds == 1200.0

    async def test_daily_aggregation_idempotent(
        self,
        billing_db: Database,
        billing_workspace,
    ):
        """Verify running aggregation twice doesn't duplicate records."""
        workspace_id = str(billing_workspace.id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        await create_interval_records(
            db=billing_db,
            workspace_id=workspace_id,
            date=yesterday,
            num_intervals=24,
        )

        day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

        # Run aggregation twice
        result1 = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        result2 = await collect_workspace_daily_usage(
            workspace_id=workspace_id,
            day_start=day_start,
            day_end=day_end,
        )

        assert result1["success"] is True
        assert result2["success"] is True

        # Should still only have one daily record
        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=day_start,
            end_date=day_end,
            record_type=UsageRecordType.DAILY,
        )

        assert len(records) == 1
