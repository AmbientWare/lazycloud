import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from models.billing import UsageUnits
from responses.usage import (
    AggregatedDailyUsageResponse,
    AggregatedUsageResponse,
    DailyUsageData,
    DeploymentUsageOverview,
    MeterCostBreakdown,
    ServiceUsageItem,
    UsageMetrics,
    UsagePeriodInfo,
    VolumeUsageItem,
    WorkspaceUsageSummary,
)

from backend.database import get_db_context
from backend.database.models import DailyUsageRecordPydantic
from backend.services.cost_breakdown_service import CostBreakdownService
from backend.services.depot_service import DepotService


def _get_calendar_day_in_timezone(utc_datetime: datetime, tz: ZoneInfo) -> str:
    local_time = utc_datetime.astimezone(tz)
    return local_time.strftime("%Y-%m-%d")


def _get_utc_midnight_for_calendar_day(calendar_day: str, tz: ZoneInfo) -> datetime:
    local_midnight = datetime.strptime(calendar_day, "%Y-%m-%d").replace(
        tzinfo=tz, hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(timezone.utc)


class UsageService:
    def __init__(
        self,
        cost_service: CostBreakdownService,
        depot_service: DepotService,
    ):
        self.cost_service = cost_service
        self.depot_service = depot_service

    async def aggregate_workspace_usage_for_date_range(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> UsageMetrics:
        """Aggregate workspace-level usage from daily records."""
        async with get_db_context() as db:
            daily_records = await db.usage.get_workspace_daily_usage(
                workspace_id=workspace_id,
                start_date=start_date.date(),
                end_date=end_date.date(),
            )

        total_cpu_seconds = sum(r.cpu_core_seconds for r in daily_records)
        total_memory_seconds = sum(r.memory_gb_seconds for r in daily_records)
        total_build_minutes = sum(r.build_minutes for r in daily_records)
        total_storage_gb_months = sum(r.storage_gb_months for r in daily_records)

        return UsageMetrics(
            cpu_core_hours=UsageUnits.seconds_to_hours(total_cpu_seconds),
            memory_gb_hours=UsageUnits.seconds_to_hours(total_memory_seconds),
            build_minutes=total_build_minutes,
            storage_gb_months=total_storage_gb_months,
        )

    async def get_deployment_breakdown(
        self,
        workspace_id: str,
        deployment_id: str,
        start_date: datetime,
        end_date: datetime,
    ) -> tuple[UsageMetrics, list[ServiceUsageItem], list[VolumeUsageItem]]:
        """Get usage breakdown for a specific deployment."""
        async with get_db_context() as db:
            (
                cpu,
                memory,
                storage_gb_hours,
                _,
                build,
            ) = await db.usage.get_deployment_usage(
                deployment_id=deployment_id,
                start_date=start_date,
                end_date=end_date,
            )

            service_breakdown = await db.usage.get_service_breakdown(
                workspace_id=workspace_id,
                start_date=start_date,
                end_date=end_date,
                deployment_id=deployment_id,
            )

            volume_breakdown = await db.usage.get_volume_breakdown(
                workspace_id=workspace_id,
                start_date=start_date,
                end_date=end_date,
                deployment_id=deployment_id,
            )

        metrics = UsageMetrics(
            cpu_core_hours=UsageUnits.seconds_to_hours(cpu),
            memory_gb_hours=UsageUnits.seconds_to_hours(memory),
            build_minutes=build,
            storage_gb_months=UsageUnits.gb_hours_to_gb_months(storage_gb_hours),
        )

        services = [
            ServiceUsageItem(
                service_name=name,
                cpu_core_seconds=cpu_secs,
                memory_gb_seconds=mem_secs,
            )
            for name, cpu_secs, mem_secs in service_breakdown
        ]

        volumes = [
            VolumeUsageItem(
                volume_name=name,
                storage_class=storage_class,
                gb_hours=gb_hours,
            )
            for name, storage_class, gb_hours in volume_breakdown
        ]

        return metrics, services, volumes

    async def get_aggregated_usage_with_summaries(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> AggregatedUsageResponse:
        """Get aggregated usage across all user's workspaces with workspace summaries."""
        async with get_db_context() as db:
            all_user_workspaces = (
                await db.workspaces.get_user_workspaces_active_during_range(
                    user_id=user_id,
                    start_date=start_date,
                    end_date=end_date,
                )
            )

        results = await asyncio.gather(
            *[
                self.aggregate_workspace_usage_for_date_range(
                    workspace.id, start_date, end_date
                )
                for workspace, _ in all_user_workspaces
            ],
            return_exceptions=True,
        )

        workspace_usage_results: list[UsageMetrics | None] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch usage: {result}", exc_info=True)
                workspace_usage_results.append(None)
            else:
                workspace_usage_results.append(result)

        cost_results = await self.cost_service.calculate_costs_batch(
            workspace_usage_results, external_customer_id
        )

        workspace_summaries, total_usage = await self._build_workspace_summaries(
            all_user_workspaces,
            workspace_usage_results,
            cost_results,
            start_date,
            end_date,
            external_customer_id=external_customer_id,
        )

        total_costs = None
        if self.cost_service.is_enabled() and cost_results:
            try:
                total_costs = MeterCostBreakdown(
                    cpu_cost=sum(c.cpu_cost for c in cost_results if c),
                    memory_cost=sum(c.memory_cost for c in cost_results if c),
                    build_cost=sum(c.build_cost for c in cost_results if c),
                    storage_cost=sum(c.storage_cost for c in cost_results if c),
                    total_cost=sum(c.total_cost for c in cost_results if c),
                )
            except Exception as e:
                logger.warning(f"Failed to calculate aggregated costs: {e}")

        total_usage.costs = total_costs

        return AggregatedUsageResponse(
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=total_usage,
            workspace_count=len(all_user_workspaces),
            record_count=len([u for u in workspace_usage_results if u]),
            workspaces=workspace_summaries,
        )

    async def get_aggregated_daily_usage(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        timezone_str: str,
        external_customer_id: str,
    ) -> AggregatedDailyUsageResponse:
        """Get aggregated daily usage across all user's workspaces."""
        tz = self._parse_timezone(timezone_str)

        async with get_db_context() as db:
            user_workspaces = (
                await db.workspaces.get_user_workspaces_active_during_range(
                    user_id=user_id,
                    start_date=start_date,
                    end_date=end_date,
                )
            )

            usage_records_tasks = [
                db.usage.get_workspace_daily_usage(
                    workspace_id=workspace.id,
                    start_date=start_date.date(),
                    end_date=end_date.date(),
                )
                for workspace, _ in user_workspaces
            ]

        all_usage_records_lists = await asyncio.gather(
            *usage_records_tasks, return_exceptions=True
        )

        daily_data: dict[str, DailyUsageData] = {}
        for usage_records in all_usage_records_lists:
            if isinstance(usage_records, Exception):
                logger.warning(
                    f"Failed to fetch workspace usage: {usage_records}", exc_info=True
                )
                continue

            workspace_daily = self._aggregate_daily_records_by_day(usage_records, tz)
            for day_key, day_data in workspace_daily.items():
                if day_key not in daily_data:
                    daily_data[day_key] = day_data
                else:
                    daily_data[day_key].cpu_core_hours += day_data.cpu_core_hours
                    daily_data[day_key].memory_gb_hours += day_data.memory_gb_hours
                    daily_data[day_key].build_minutes += day_data.build_minutes
                    daily_data[day_key].storage_gb_months += day_data.storage_gb_months

        self._fill_missing_days(daily_data, start_date, end_date, tz)
        await self.cost_service.calculate_costs_for_daily_data(
            daily_data, external_customer_id
        )

        sorted_daily = sorted(daily_data.values(), key=lambda x: x.date)

        return AggregatedDailyUsageResponse(
            period=UsagePeriodInfo(start=start_date, end=end_date),
            daily_usage=sorted_daily,
            workspace_count=len(user_workspaces),
        )

    async def _build_deployment_overviews(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> list[DeploymentUsageOverview]:
        """Build deployment overviews for a workspace with actual usage metrics."""
        async with get_db_context() as db:
            deployments = await db.compose_deployments.find_active_during_date_range(
                workspace_id=workspace_id,
                start_date=start_date,
                end_date=end_date,
                limit=100,
            )

        if not deployments:
            return []

        deployment_metrics_map: dict[str, UsageMetrics] = {}
        valid_deployments = []

        for deployment in deployments:
            if not deployment.id or not deployment.name:
                continue

            valid_deployments.append(deployment)
            metrics, _, _ = await self.get_deployment_breakdown(
                workspace_id=workspace_id,
                deployment_id=deployment.id,
                start_date=start_date,
                end_date=end_date,
            )
            deployment_metrics_map[deployment.id] = metrics

        deployment_metrics_list = list(deployment_metrics_map.values())
        cost_results = await self.cost_service.calculate_costs_batch(
            usages=deployment_metrics_list,
            external_customer_id=external_customer_id,
        )

        overviews: list[DeploymentUsageOverview] = []
        cost_idx = 0
        for deployment in valid_deployments:
            deployment_id = deployment.id
            deployment_status = (
                "Active" if deployment.deleted_at is None else "Inactive"
            )

            metrics = deployment_metrics_map[deployment_id]

            if cost_idx < len(cost_results) and cost_results[cost_idx] is not None:
                metrics.costs = cost_results[cost_idx]
            cost_idx += 1

            overviews.append(
                DeploymentUsageOverview(
                    deployment_id=deployment_id,
                    deployment_name=deployment.name,
                    usage=metrics,
                    status=deployment_status,
                    deployed_at=deployment.deployed_at,
                    deleted_at=deployment.deleted_at,
                )
            )

        return overviews

    async def _build_workspace_summaries(
        self,
        workspaces: list,
        usages: list[UsageMetrics | None],
        cost_results: list[MeterCostBreakdown | None],
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> tuple[list[WorkspaceUsageSummary], UsageMetrics]:
        """Build workspace summaries and calculate totals."""
        workspace_summaries: list[WorkspaceUsageSummary] = []
        total_cpu_hours = 0.0
        total_memory_hours = 0.0
        total_build_minutes = 0.0
        total_storage_gb_months = 0.0

        deployment_overview_tasks = []
        valid_workspace_indices = []
        cost_idx = 0

        for i, (workspace, _) in enumerate(workspaces):
            usage = usages[i]
            if usage is None:
                continue

            if cost_idx < len(cost_results):
                usage.costs = cost_results[cost_idx]
                cost_idx += 1

            valid_workspace_indices.append(i)
            deployment_overview_tasks.append(
                self._build_deployment_overviews(
                    workspace.id,
                    start_date,
                    end_date,
                    external_customer_id=external_customer_id,
                )
            )

        deployment_overview_results = await asyncio.gather(
            *deployment_overview_tasks, return_exceptions=True
        )

        for idx, i in enumerate(valid_workspace_indices):
            workspace, _ = workspaces[i]
            usage = usages[i]

            deployment_overviews = deployment_overview_results[idx]
            if isinstance(deployment_overviews, Exception):
                logger.warning(
                    f"Failed to build deployment overviews for workspace {workspace.id}: {deployment_overviews}",
                    exc_info=True,
                )
                deployment_overviews = []

            workspace_status = "Active" if workspace.deleted_at is None else "Inactive"

            workspace_summaries.append(
                WorkspaceUsageSummary(
                    workspace_id=workspace.id,
                    workspace_name=workspace.name or "Unnamed Workspace",
                    workspace_status=workspace_status,
                    usage=usage,
                    record_count=1,
                    deployments=deployment_overviews,
                )
            )

            total_cpu_hours += usage.cpu_core_hours
            total_memory_hours += usage.memory_gb_hours
            total_build_minutes += usage.build_minutes
            total_storage_gb_months += usage.storage_gb_months

        total_usage = UsageMetrics(
            cpu_core_hours=total_cpu_hours,
            memory_gb_hours=total_memory_hours,
            build_minutes=total_build_minutes,
            storage_gb_months=total_storage_gb_months,
        )

        return workspace_summaries, total_usage

    def _parse_timezone(self, timezone_str: str) -> ZoneInfo:
        try:
            return ZoneInfo(timezone_str) if timezone_str else ZoneInfo("UTC")
        except Exception:
            logger.warning(f"Invalid timezone '{timezone_str}', defaulting to UTC")
            return ZoneInfo("UTC")

    def _aggregate_daily_records_by_day(
        self,
        daily_records: list[DailyUsageRecordPydantic],
        tz: ZoneInfo,
    ) -> dict[str, DailyUsageData]:
        """Aggregate daily records by calendar day in the given timezone."""
        daily_data: dict[str, DailyUsageData] = {}
        for record in daily_records:
            day_key = str(record.usage_date)
            if day_key not in daily_data:
                utc_midnight = _get_utc_midnight_for_calendar_day(day_key, tz)
                daily_data[day_key] = DailyUsageData(
                    date=utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    build_minutes=0.0,
                )

            daily_data[day_key].cpu_core_hours += UsageUnits.seconds_to_hours(
                record.cpu_core_seconds
            )
            daily_data[day_key].memory_gb_hours += UsageUnits.seconds_to_hours(
                record.memory_gb_seconds
            )
            daily_data[day_key].build_minutes += record.build_minutes
            daily_data[day_key].storage_gb_months += record.storage_gb_months

        return daily_data

    def _fill_missing_days(
        self,
        daily_data: dict[str, DailyUsageData],
        start_date: datetime,
        end_date: datetime,
        tz: ZoneInfo,
    ) -> None:
        """Fill in missing days with zero usage entries."""
        now = datetime.now(timezone.utc)
        now_local = now.astimezone(tz)
        start_local = start_date.astimezone(tz).date()
        end_local = min(end_date.astimezone(tz).date(), now_local.date())
        current_local_date = start_local

        while current_local_date <= end_local:
            day_key = current_local_date.strftime("%Y-%m-%d")
            if day_key not in daily_data:
                utc_midnight = _get_utc_midnight_for_calendar_day(day_key, tz)
                daily_data[day_key] = DailyUsageData(
                    date=utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    build_minutes=0.0,
                )
            current_local_date += timedelta(days=1)
