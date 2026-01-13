"""Tests for interval usage collection logic.

These tests mock the Prometheus service to test our transformation and
persistence logic with known, deterministic values.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from backend.database import Database
from backend.tasks.crons.usage import collect_workspace_interval
from models.metrics import (
    NamespaceBreakdown,
    PodUsage,
    UsagePeriod,
    UsageTotals,
)

from tests.fixtures.database import requires_db

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.billing,
    requires_db,
]


def make_namespace_breakdown(
    namespace: str,
    start_time: datetime,
    end_time: datetime,
    cpu_core_seconds: float = 0.0,
    memory_gb_seconds: float = 0.0,
    pods: list[PodUsage] | None = None,
) -> NamespaceBreakdown:
    """Create a mock NamespaceBreakdown for testing."""
    return NamespaceBreakdown(
        namespace=namespace,
        period=UsagePeriod(
            start=start_time,
            end=end_time,
            duration_seconds=(end_time - start_time).total_seconds(),
        ),
        totals=UsageTotals(
            cpu_core_seconds=cpu_core_seconds,
            memory_gb_seconds=memory_gb_seconds,
        ),
        by_pod=pods or [],
    )


@pytest.fixture
def mock_prometheus_healthy():
    """Mock Prometheus as healthy."""
    with patch("backend.tasks.crons.usage.get_metrics_service") as mock_get:
        mock_service = AsyncMock()
        mock_service.health_check = AsyncMock(return_value=True)
        mock_get.return_value = mock_service
        yield mock_service


@pytest.fixture
def mock_depot_service():
    """Mock Depot service (no build minutes)."""
    with patch("backend.tasks.crons.usage.get_depot_service") as mock_get:
        mock_service = AsyncMock()
        mock_service.is_configured = False
        mock_get.return_value = mock_service
        yield mock_service


@pytest.fixture
def mock_storage_collection():
    """Mock storage collection to return empty list."""
    with patch("backend.tasks.crons.usage.collect_storage_usage") as mock:
        mock.return_value = []
        yield mock


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


class TestIntervalCollection:
    """Test interval collection with atomic increments."""

    async def test_creates_daily_record_on_first_collection(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify daily record is created with correct initial values."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(minutes=15)
        expected_cpu = 100.0
        expected_memory = 200.0

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=expected_cpu,
                memory_gb_seconds=expected_memory,
            )
        )

        result = await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=start_time,
            interval_end=end_time,
        )

        assert result["status"] == "collected"
        assert result["workspace_id"] == workspace_id

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=start_time.date(),
            end_date=start_time.date(),
        )

        assert len(records) == 1
        record = records[0]
        assert record.cpu_core_seconds == expected_cpu
        assert record.memory_gb_seconds == expected_memory
        assert record.intervals_collected == 1
        assert record.usage_date == start_time.date()

    async def test_increments_existing_daily_record(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify multiple intervals accumulate correctly in daily record."""
        workspace_id = str(billing_workspace.id)
        base_time = datetime.now(timezone.utc).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

        # First interval: 100 CPU, 200 memory
        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=base_time,
                end_time=base_time + timedelta(minutes=15),
                cpu_core_seconds=100.0,
                memory_gb_seconds=200.0,
            )
        )

        await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=base_time,
            interval_end=base_time + timedelta(minutes=15),
        )

        # Second interval: 150 CPU, 300 memory
        interval2_start = base_time + timedelta(minutes=15)
        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=interval2_start,
                end_time=interval2_start + timedelta(minutes=15),
                cpu_core_seconds=150.0,
                memory_gb_seconds=300.0,
            )
        )

        await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=interval2_start,
            interval_end=interval2_start + timedelta(minutes=15),
        )

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=base_time.date(),
            end_date=base_time.date(),
        )

        assert len(records) == 1
        record = records[0]
        # Verify atomic increment worked: 100 + 150 = 250
        assert record.cpu_core_seconds == 250.0
        assert record.memory_gb_seconds == 500.0
        assert record.intervals_collected == 2


class TestIdempotency:
    """Test that collection is idempotent - same interval never counted twice."""

    async def test_skips_already_collected_interval(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify duplicate collection returns already_collected and doesn't increment."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(minutes=15)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=100.0,
            )
        )

        # First collection
        result1 = await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=start_time,
            interval_end=end_time,
        )
        assert result1["status"] == "collected"

        # Second collection of same interval
        result2 = await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=start_time,
            interval_end=end_time,
        )
        assert result2["status"] == "already_collected"

        # Verify no double-counting occurred
        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=start_time.date(),
            end_date=start_time.date(),
        )

        assert len(records) == 1
        assert records[0].cpu_core_seconds == 100.0  # Not 200
        assert records[0].intervals_collected == 1  # Not 2

    async def test_idempotency_tracked_per_interval(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify different intervals are tracked independently."""
        workspace_id = str(billing_workspace.id)
        base_time = datetime.now(timezone.utc).replace(
            hour=12, minute=0, second=0, microsecond=0
        )

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=base_time,
                end_time=base_time + timedelta(minutes=15),
                cpu_core_seconds=50.0,
            )
        )

        # Collect interval 1
        await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=base_time,
            interval_end=base_time + timedelta(minutes=15),
        )

        # Collect interval 2 (different time)
        interval2_start = base_time + timedelta(minutes=15)
        result = await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=interval2_start,
            interval_end=interval2_start + timedelta(minutes=15),
        )

        # Should be collected, not skipped
        assert result["status"] == "collected"

        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=base_time.date(),
            end_date=base_time.date(),
        )
        assert records[0].intervals_collected == 2


class TestBreakdownEvents:
    """Test that breakdown events are appended correctly for dashboards."""

    async def test_appends_compute_breakdown_events(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify per-service breakdown events are stored with correct values."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(minutes=15)

        pods = [
            PodUsage(
                pod="web-0",
                service="web",
                cpu_core_seconds=50.0,
                memory_gb_seconds=100.0,
            ),
            PodUsage(
                pod="worker-0",
                service="worker",
                cpu_core_seconds=75.0,
                memory_gb_seconds=150.0,
            ),
        ]

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=125.0,
                memory_gb_seconds=250.0,
                pods=pods,
            )
        )

        await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=start_time,
            interval_end=end_time,
        )

        # Query and verify breakdown events
        service_breakdown = await billing_db.usage.get_service_breakdown(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time + timedelta(seconds=1),
        )

        assert len(service_breakdown) == 2
        services = {name: (cpu, mem) for name, cpu, mem in service_breakdown}

        assert "web" in services
        assert services["web"][0] == 50.0  # CPU
        assert services["web"][1] == 100.0  # Memory

        assert "worker" in services
        assert services["worker"][0] == 75.0  # CPU
        assert services["worker"][1] == 150.0  # Memory


class TestBilledDayProtection:
    """Test that billed days cannot be modified."""

    async def test_skips_collection_for_billed_day(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify collection returns already_billed and doesn't modify billed record."""
        workspace_id = str(billing_workspace.id)
        usage_date = datetime.now(timezone.utc).date()

        # Create and bill a daily record
        record = await billing_db.usage.get_or_create_daily_record(
            workspace_id=workspace_id,
            usage_date=usage_date,
        )
        original_cpu = record.cpu_core_seconds
        await billing_db.usage.mark_as_billed(record.id, "test-billing-id")

        start_time = datetime.combine(usage_date, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_time = start_time + timedelta(minutes=15)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=9999.0,  # This should NOT be added
            )
        )

        result = await collect_workspace_interval(
            workspace_id=workspace_id,
            interval_start=start_time,
            interval_end=end_time,
        )

        assert result["status"] == "already_billed"

        # Verify record was not modified
        records = await billing_db.usage.get_workspace_daily_usage(
            workspace_id=workspace_id,
            start_date=usage_date,
            end_date=usage_date,
        )
        assert records[0].cpu_core_seconds == original_cpu


class TestErrorHandling:
    """Test error handling scenarios."""

    async def test_fails_when_prometheus_unhealthy(
        self,
        billing_db: Database,
        billing_workspace,
        mock_depot_service,
        mock_storage_collection,
    ):
        """Verify collection raises RuntimeError when Prometheus is down."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(minutes=15)

        with patch(
            "backend.tasks.crons.usage.get_metrics_service"
        ) as mock_get:
            mock_service = AsyncMock()
            mock_service.health_check = AsyncMock(return_value=False)
            mock_get.return_value = mock_service

            with pytest.raises(RuntimeError) as exc_info:
                await collect_workspace_interval(
                    workspace_id=workspace_id,
                    interval_start=start_time,
                    interval_end=end_time,
                )

            assert "Prometheus unhealthy" in str(exc_info.value)
            assert workspace_id in str(exc_info.value)
