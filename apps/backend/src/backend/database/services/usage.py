from datetime import date, datetime, timezone

from models.billing import UsageCollectionConfig
from models.storage import STORAGE_CLASS_STANDARD
from sqlalchemy import (
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    BreakdownType,
    DailyUsageRecordPydantic,
    DailyUsageStatus,
)
from backend.database.services.base import DatabaseService
from backend.database.tables import (
    CollectedIntervalTable,
    DailyUsageRecordTable,
    UsageBreakdownEventTable,
)


class UsageService(DatabaseService[DailyUsageRecordTable, DailyUsageRecordPydantic]):
    def __init__(self, session: AsyncSession):
        super().__init__(DailyUsageRecordTable, DailyUsageRecordPydantic, session)

    async def is_interval_collected(
        self, workspace_id: str, interval_start: datetime
    ) -> bool:
        """Check if an interval has already been collected."""
        result = await self._session.execute(
            select(CollectedIntervalTable)
            .where(CollectedIntervalTable.workspace_id == workspace_id)
            .where(CollectedIntervalTable.interval_start == interval_start)
        )
        return result.scalar_one_or_none() is not None

    async def get_or_create_daily_record(
        self, workspace_id: str, usage_date: date, expected_intervals: int = 96
    ) -> DailyUsageRecordPydantic:
        """Get existing daily record or create a new one."""
        result = await self._session.execute(
            select(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.workspace_id == workspace_id)
            .where(DailyUsageRecordTable.usage_date == usage_date)
        )
        record = result.scalar_one_or_none()

        if record:
            return record.to_pydantic(DailyUsageRecordPydantic)

        new_record = DailyUsageRecordTable(
            workspace_id=workspace_id,
            usage_date=usage_date,
            status=DailyUsageStatus.COLLECTING.value,
            expected_intervals=expected_intervals,
        )
        self._session.add(new_record)
        await self._session.flush()
        await self._session.refresh(new_record)
        return new_record.to_pydantic(DailyUsageRecordPydantic)

    async def atomic_increment_usage(
        self,
        record_id: str,
        cpu_core_seconds: float,
        memory_gb_seconds: float,
        build_minutes: float,
        storage_gb_months: float = 0.0,
    ) -> None:
        """Atomically increment usage totals - no race conditions."""
        await self._session.execute(
            update(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.id == record_id)
            .values(
                cpu_core_seconds=DailyUsageRecordTable.cpu_core_seconds
                + cpu_core_seconds,
                memory_gb_seconds=DailyUsageRecordTable.memory_gb_seconds
                + memory_gb_seconds,
                build_minutes=DailyUsageRecordTable.build_minutes + build_minutes,
                storage_gb_months=DailyUsageRecordTable.storage_gb_months
                + storage_gb_months,
                intervals_collected=DailyUsageRecordTable.intervals_collected + 1,
                updated_at=func.now(),
            )
        )

    async def mark_interval_collected(
        self, workspace_id: str, interval_start: datetime
    ) -> None:
        """Mark an interval as collected for idempotency."""
        interval = CollectedIntervalTable(
            workspace_id=workspace_id,
            interval_start=interval_start,
        )
        self._session.add(interval)
        await self._session.flush()

    async def add_breakdown_event(
        self,
        workspace_id: str,
        interval_start: datetime,
        interval_end: datetime,
        breakdown_type: BreakdownType,
        resource_name: str,
        deployment_id: str | None = None,
        service_name: str | None = None,
        storage_class: str | None = None,
        cpu_core_seconds: float = 0.0,
        memory_gb_seconds: float = 0.0,
        gb_hours: float = 0.0,
        endpoint_hours: float = 0.0,
        build_minutes: float = 0.0,
    ) -> None:
        """Append a breakdown event for dashboard queries."""
        event = UsageBreakdownEventTable(
            workspace_id=workspace_id,
            deployment_id=deployment_id,
            interval_start=interval_start,
            interval_end=interval_end,
            breakdown_type=breakdown_type.value,
            resource_name=resource_name,
            service_name=service_name,
            storage_class=storage_class,
            cpu_core_seconds=cpu_core_seconds,
            memory_gb_seconds=memory_gb_seconds,
            gb_hours=gb_hours,
            endpoint_hours=endpoint_hours,
            build_minutes=build_minutes,
        )
        self._session.add(event)

    async def get_unbilled_for_date(
        self, usage_date: date
    ) -> list[DailyUsageRecordPydantic]:
        """Get all daily records that are collecting and ready to bill."""
        result = await self._session.execute(
            select(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.status == DailyUsageStatus.COLLECTING.value)
            .where(DailyUsageRecordTable.usage_date == usage_date)
            .order_by(DailyUsageRecordTable.workspace_id)
        )
        records = result.scalars().all()
        return [r.to_pydantic(DailyUsageRecordPydantic) for r in records]

    async def mark_as_billed(self, record_id: str, billing_id: str) -> None:
        """Mark a daily record as billed."""
        await self._session.execute(
            update(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.id == record_id)
            .values(
                status=DailyUsageStatus.BILLED.value,
                billing_id=billing_id,
                billed_at=datetime.now(timezone.utc),
                updated_at=func.now(),
            )
        )

    async def increment_billing_attempt(
        self, record_id: str, error: str | None = None
    ) -> None:
        """Increment billing attempt counter and optionally record error."""
        values = {
            "billing_attempts": DailyUsageRecordTable.billing_attempts + 1,
            "last_billing_attempt_at": datetime.now(timezone.utc),
            "updated_at": func.now(),
        }
        if error:
            values["last_billing_error"] = error[:500]
        await self._session.execute(
            update(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.id == record_id)
            .values(**values)
        )

    async def get_stuck_records(
        self, before_date: date
    ) -> list[DailyUsageRecordPydantic]:
        """Get records stuck in collecting status from before the given date."""
        result = await self._session.execute(
            select(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.status == DailyUsageStatus.COLLECTING.value)
            .where(DailyUsageRecordTable.usage_date < before_date)
            .order_by(DailyUsageRecordTable.usage_date)
        )
        records = result.scalars().all()
        return [r.to_pydantic(DailyUsageRecordPydantic) for r in records]

    async def get_workspace_daily_usage(
        self,
        workspace_id: str,
        start_date: date,
        end_date: date,
    ) -> list[DailyUsageRecordPydantic]:
        """Get daily usage records for a workspace in a date range."""
        result = await self._session.execute(
            select(DailyUsageRecordTable)
            .where(DailyUsageRecordTable.workspace_id == workspace_id)
            .where(DailyUsageRecordTable.usage_date >= start_date)
            .where(DailyUsageRecordTable.usage_date <= end_date)
            .order_by(DailyUsageRecordTable.usage_date)
        )
        records = result.scalars().all()
        return [r.to_pydantic(DailyUsageRecordPydantic) for r in records]

    async def get_service_breakdown(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        deployment_id: str | None = None,
    ) -> list[tuple[str, float, float]]:
        """Aggregate compute usage by service for dashboards."""
        query = (
            select(
                UsageBreakdownEventTable.service_name,
                func.sum(UsageBreakdownEventTable.cpu_core_seconds).label("total_cpu"),
                func.sum(UsageBreakdownEventTable.memory_gb_seconds).label(
                    "total_memory"
                ),
            )
            .where(UsageBreakdownEventTable.workspace_id == workspace_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.COMPUTE.value
            )
            .where(UsageBreakdownEventTable.interval_start >= start_date)
            .where(UsageBreakdownEventTable.interval_start < end_date)
            .where(UsageBreakdownEventTable.service_name.isnot(None))
            .group_by(UsageBreakdownEventTable.service_name)
        )

        if deployment_id:
            query = query.where(UsageBreakdownEventTable.deployment_id == deployment_id)

        result = await self._session.execute(query)
        return [(row[0], row[1] or 0.0, row[2] or 0.0) for row in result.all()]

    async def get_volume_breakdown(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        deployment_id: str | None = None,
    ) -> list[tuple[str, str, float]]:
        """Aggregate storage usage by volume for dashboards."""
        query = (
            select(
                UsageBreakdownEventTable.resource_name,
                UsageBreakdownEventTable.storage_class,
                func.sum(UsageBreakdownEventTable.gb_hours).label("total_gb_hours"),
            )
            .where(UsageBreakdownEventTable.workspace_id == workspace_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.STORAGE.value
            )
            .where(UsageBreakdownEventTable.interval_start >= start_date)
            .where(UsageBreakdownEventTable.interval_start < end_date)
            .group_by(
                UsageBreakdownEventTable.resource_name,
                UsageBreakdownEventTable.storage_class,
            )
        )

        if deployment_id:
            query = query.where(UsageBreakdownEventTable.deployment_id == deployment_id)

        result = await self._session.execute(query)
        return [(row[0], row[1] or "", row[2] or 0.0) for row in result.all()]

    async def get_deployment_usage(
        self,
        deployment_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> tuple[float, float, float, float, float]:
        """Get aggregated usage for a specific deployment."""
        compute_query = (
            select(
                func.sum(UsageBreakdownEventTable.cpu_core_seconds).label("cpu"),
                func.sum(UsageBreakdownEventTable.memory_gb_seconds).label("memory"),
            )
            .where(UsageBreakdownEventTable.deployment_id == deployment_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.COMPUTE.value
            )
            .where(UsageBreakdownEventTable.interval_start >= start_date)
            .where(UsageBreakdownEventTable.interval_start < end_date)
        )

        storage_query = (
            select(
                func.sum(UsageBreakdownEventTable.gb_hours).label("gb_hours"),
            )
            .where(UsageBreakdownEventTable.deployment_id == deployment_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.STORAGE.value
            )
            .where(UsageBreakdownEventTable.interval_start >= start_date)
            .where(UsageBreakdownEventTable.interval_start < end_date)
        )

        network_query = (
            select(
                func.sum(UsageBreakdownEventTable.endpoint_hours).label(
                    "endpoint_hours"
                )
            )
            .where(UsageBreakdownEventTable.deployment_id == deployment_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.NETWORK.value
            )
            .where(UsageBreakdownEventTable.interval_start >= start_date)
            .where(UsageBreakdownEventTable.interval_start < end_date)
        )

        build_query = (
            select(
                func.sum(UsageBreakdownEventTable.build_minutes).label("build_minutes")
            )
            .where(UsageBreakdownEventTable.deployment_id == deployment_id)
            .where(UsageBreakdownEventTable.breakdown_type == BreakdownType.BUILD.value)
            .where(UsageBreakdownEventTable.interval_start >= start_date)
            .where(UsageBreakdownEventTable.interval_start < end_date)
        )

        compute_result = await self._session.execute(compute_query)
        storage_result = await self._session.execute(storage_query)
        network_result = await self._session.execute(network_query)
        build_result = await self._session.execute(build_query)

        compute_row = compute_result.one()
        cpu = compute_row[0] or 0.0
        memory = compute_row[1] or 0.0

        storage_gb_hours = storage_result.scalar() or 0.0

        endpoint_hours = network_result.scalar() or 0.0
        build_minutes = build_result.scalar() or 0.0

        return (
            cpu,
            memory,
            storage_gb_hours,
            endpoint_hours,
            build_minutes,
        )

    async def get_latest_storage_sizes(
        self,
        deployment_id: str,
    ) -> dict[str, tuple[str, float]]:
        """Get latest storage sizes for a deployment from breakdown events.

        Returns a dict mapping volume name to (storage_class, size_gb).
        Size is calculated from gb_hours / interval_hours where interval is 15 minutes.
        For EBS: this is the provisioned size.
        For EFS: this is the actual used size from CloudWatch.
        """
        # Get the most recent interval for this deployment
        latest_interval_query = (
            select(func.max(UsageBreakdownEventTable.interval_start))
            .where(UsageBreakdownEventTable.deployment_id == deployment_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.STORAGE.value
            )
        )
        latest_interval = (await self._session.execute(latest_interval_query)).scalar()

        if not latest_interval:
            return {}

        # Get all storage events from that interval
        query = (
            select(
                UsageBreakdownEventTable.resource_name,
                UsageBreakdownEventTable.storage_class,
                UsageBreakdownEventTable.gb_hours,
            )
            .where(UsageBreakdownEventTable.deployment_id == deployment_id)
            .where(
                UsageBreakdownEventTable.breakdown_type == BreakdownType.STORAGE.value
            )
            .where(UsageBreakdownEventTable.interval_start == latest_interval)
        )

        result = await self._session.execute(query)

        # Convert gb_hours back to size_gb
        # gb_hours = size_gb * interval_hours, so size_gb = gb_hours / interval_hours
        interval_hours = UsageCollectionConfig.COLLECTION_INTERVAL.value / 60.0

        storage_sizes: dict[str, tuple[str, float]] = {}
        for row in result.all():
            volume_name = row[0]
            storage_class = row[1] or STORAGE_CLASS_STANDARD
            gb_hours = row[2] or 0.0
            size_gb = gb_hours / interval_hours if interval_hours > 0 else 0.0
            storage_sizes[volume_name] = (storage_class, size_gb)

        return storage_sizes
