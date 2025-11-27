"""Tests for interval usage collection logic.

These tests mock the Prometheus service to test our transformation and
persistence logic with known, deterministic values. This isolates our
business logic from external infrastructure timing issues.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from lazycloud_api.database import Database
from lazycloud_api.prefect_app.usage_collector import (
    collect_workspace_usage_for_interval,
)
from lazycloud_api.tests.fixtures.database import requires_db
from shared.models.billing import (
    UsageCollectionConfig,
    UsageRecordStatus,
)
from shared.models.metrics import (
    NamespaceBreakdown,
    PodUsage,
    StorageUsage,
    UsagePeriod,
    UsageTotals,
)

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
    standard_gb_hours: float = 0.0,
    shared_gb_hours: float = 0.0,
    pods: list[PodUsage] | None = None,
    pvcs: list[StorageUsage] | None = None,
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
            storage_gb_hours=standard_gb_hours + shared_gb_hours,
            standard_gb_hours=standard_gb_hours,
            shared_gb_hours=shared_gb_hours,
        ),
        by_pod=pods or [],
        by_pvc=pvcs or [],
    )


@pytest.fixture
def mock_prometheus_healthy():
    """Mock Prometheus as healthy."""
    with patch(
        "lazycloud_api.prefect_app.usage_collector.get_metrics_service"
    ) as mock_get:
        mock_service = AsyncMock()
        mock_service.health_check = AsyncMock(return_value=True)
        mock_get.return_value = mock_service
        yield mock_service


@pytest.fixture
def mock_depot_service():
    """Mock Depot service (no build minutes)."""
    with patch(
        "lazycloud_api.prefect_app.usage_collector.get_depot_service"
    ) as mock_get:
        mock_service = AsyncMock()
        mock_service.is_configured = False
        mock_get.return_value = mock_service
        yield mock_service


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
        "lazycloud_api.prefect_app.usage_collector.get_db_context",
        side_effect=get_mock_db_context,
    ):
        yield billing_db


class TestIntervalCollectionTransformation:
    """Test that Prometheus data is correctly transformed and stored."""

    async def test_stores_cpu_core_seconds_from_prometheus(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify CPU core-seconds from Prometheus are stored exactly."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        expected_cpu = 3600.0  # 1 core for 1 hour

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=expected_cpu,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].cpu_core_seconds == expected_cpu

    async def test_stores_memory_gb_seconds_from_prometheus(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify memory GB-seconds from Prometheus are stored exactly."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        expected_memory = 7200.0  # 2 GB for 1 hour

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                memory_gb_seconds=expected_memory,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].memory_gb_seconds == expected_memory

    async def test_stores_storage_split_by_class(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify EBS and EFS storage are stored in separate fields."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        expected_standard = 10.0
        expected_shared = 5.0

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                standard_gb_hours=expected_standard,
                shared_gb_hours=expected_shared,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].standard_gb_hours == expected_standard
        assert records[0].shared_gb_hours == expected_shared

    async def test_stores_all_metrics_together(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify all metrics are stored correctly in single record."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        expected_cpu = 1800.0
        expected_memory = 3600.0
        expected_standard = 2.5
        expected_shared = 1.5

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=expected_cpu,
                memory_gb_seconds=expected_memory,
                standard_gb_hours=expected_standard,
                shared_gb_hours=expected_shared,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        record = records[0]
        assert record.cpu_core_seconds == expected_cpu
        assert record.memory_gb_seconds == expected_memory
        assert record.standard_gb_hours == expected_standard
        assert record.shared_gb_hours == expected_shared


class TestIntervalCollectionBreakdowns:
    """Test that per-pod breakdowns are stored correctly."""

    async def test_stores_compute_breakdown_per_pod(
        self,
        billing_db: Database,
        billing_workspace,
        billing_deployment,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify each pod gets a breakdown record with correct values."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        pods = [
            PodUsage(
                pod="web-0",
                service="web",
                release_name=f"lc-{workspace_id[:8]}-test",
                cpu_core_seconds=1000.0,
                memory_gb_seconds=2000.0,
            ),
            PodUsage(
                pod="web-1",
                service="web",
                release_name=f"lc-{workspace_id[:8]}-test",
                cpu_core_seconds=1500.0,
                memory_gb_seconds=3000.0,
            ),
            PodUsage(
                pod="worker-0",
                service="worker",
                release_name=f"lc-{workspace_id[:8]}-test",
                cpu_core_seconds=500.0,
                memory_gb_seconds=1000.0,
            ),
        ]

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=3000.0,
                memory_gb_seconds=6000.0,
                pods=pods,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        record = records[0]

        # Should have 3 breakdown records
        assert len(record.compute_breakdowns) == 3

        # Verify each pod's values
        breakdown_by_pod = {b.pod_name: b for b in record.compute_breakdowns}

        assert "web-0" in breakdown_by_pod
        assert breakdown_by_pod["web-0"].cpu_core_seconds == 1000.0
        assert breakdown_by_pod["web-0"].memory_gb_seconds == 2000.0

        assert "web-1" in breakdown_by_pod
        assert breakdown_by_pod["web-1"].cpu_core_seconds == 1500.0
        assert breakdown_by_pod["web-1"].memory_gb_seconds == 3000.0

        assert "worker-0" in breakdown_by_pod
        assert breakdown_by_pod["worker-0"].cpu_core_seconds == 500.0
        assert breakdown_by_pod["worker-0"].memory_gb_seconds == 1000.0

    async def test_stores_storage_breakdown_per_pvc(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify each PVC gets a storage breakdown record."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        pvcs = [
            StorageUsage(pvc_name="data-vol", storage_class="ebs-sc", gb_hours=10.0),
            StorageUsage(pvc_name="shared-vol", storage_class="efs-sc", gb_hours=5.0),
        ]

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                standard_gb_hours=10.0,
                shared_gb_hours=5.0,
                pvcs=pvcs,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        record = records[0]

        # Should have 2 storage breakdown records
        assert len(record.storage_breakdowns) == 2

        breakdown_by_pvc = {b.pvc_name: b for b in record.storage_breakdowns}

        assert "data-vol" in breakdown_by_pvc
        assert breakdown_by_pvc["data-vol"].storage_class == "ebs-sc"
        assert breakdown_by_pvc["data-vol"].gb_hours == 10.0

        assert "shared-vol" in breakdown_by_pvc
        assert breakdown_by_pvc["shared-vol"].storage_class == "efs-sc"
        assert breakdown_by_pvc["shared-vol"].gb_hours == 5.0


class TestIntervalCollectionMetadata:
    """Test record metadata is set correctly."""

    async def test_sets_correct_time_window(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify collection_start and collection_end match input."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime(2024, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2024, 6, 15, 15, 0, 0, tzinfo=timezone.utc)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
            )
        )

        await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].collection_start == start_time
        assert records[0].collection_end == end_time

    async def test_sets_interval_record_type(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify record_type matches the configured collection interval."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
            )
        )

        await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].record_type == UsageCollectionConfig.get_record_type().value

    async def test_respects_status_parameter(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify status parameter is applied to record."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
            )
        )

        await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
            status=UsageRecordStatus.FINALIZED,
        )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].status == UsageRecordStatus.FINALIZED


class TestIntervalCollectionUpsert:
    """Test upsert behavior (idempotent collection)."""

    async def test_upsert_returns_same_record_id(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify re-collection updates existing record, not creates new."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=1000.0,
            )
        )

        # First collection
        result1 = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )
        first_id = result1["usage_record_id"]

        # Update mock to return different values
        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=2000.0,  # Different value
            )
        )

        # Second collection (same time window)
        result2 = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )
        second_id = result2["usage_record_id"]

        assert first_id == second_id

    async def test_upsert_updates_values(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify upsert updates the stored values."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        # First collection with initial values
        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=1000.0,
                memory_gb_seconds=2000.0,
            )
        )

        await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        # Update mock to return different values
        new_cpu = 1500.0
        new_memory = 3000.0
        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
                cpu_core_seconds=new_cpu,
                memory_gb_seconds=new_memory,
            )
        )

        # Second collection
        await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        # Should still be one record
        assert len(records) == 1
        # Values should be updated
        assert records[0].cpu_core_seconds == new_cpu
        assert records[0].memory_gb_seconds == new_memory

    async def test_only_one_record_per_time_window(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify multiple collections for same window don't create duplicates."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
            )
        )

        # Collect 5 times
        for _ in range(5):
            await collect_workspace_usage_for_interval(
                workspace_id=workspace_id,
                start_time=start_time,
                end_time=end_time,
            )

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1


class TestIntervalCollectionErrorHandling:
    """Test error handling scenarios."""

    async def test_fails_when_prometheus_unhealthy(
        self,
        billing_db: Database,
        billing_workspace,
        mock_depot_service,
    ):
        """Verify collection fails gracefully when Prometheus is down."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        with patch(
            "lazycloud_api.prefect_app.usage_collector.get_metrics_service"
        ) as mock_get:
            mock_service = AsyncMock()
            mock_service.health_check = AsyncMock(return_value=False)
            mock_get.return_value = mock_service

            result = await collect_workspace_usage_for_interval(
                workspace_id=workspace_id,
                start_time=start_time,
                end_time=end_time,
            )

        assert result["success"] is False
        assert "Prometheus unhealthy" in result.get("error", "")


class TestIntervalCollectionEndpointHours:
    """Test public endpoint hour calculation."""

    async def test_no_endpoints_yields_zero_hours(
        self,
        billing_db: Database,
        billing_workspace,
        mock_prometheus_healthy,
        mock_depot_service,
    ):
        """Verify zero endpoint hours when no public endpoints exist."""
        workspace_id = str(billing_workspace.id)
        start_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = start_time + timedelta(hours=1)

        mock_prometheus_healthy.get_namespace_breakdown = AsyncMock(
            return_value=make_namespace_breakdown(
                namespace=f"lc-{workspace_id[:8]}",
                start_time=start_time,
                end_time=end_time,
            )
        )

        result = await collect_workspace_usage_for_interval(
            workspace_id=workspace_id,
            start_time=start_time,
            end_time=end_time,
        )

        assert result["success"] is True

        records = await billing_db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_time,
            end_date=end_time,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        assert len(records) == 1
        assert records[0].public_endpoint_hours == 0.0
