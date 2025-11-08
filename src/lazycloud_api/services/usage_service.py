import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic
from lazycloud_api.services.cost_breakdown_service import CostBreakdownService
from shared.models.billing import (
    SECONDS_PER_HOUR,
    STORAGE_CLASS_EFS,
    STORAGE_CLASS_S3,
    UsageCollectionConfig,
)
from shared.responses.usage import (
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


def _get_calendar_day_in_timezone(utc_datetime: datetime, tz: ZoneInfo) -> str:
    """Get the calendar day (YYYY-MM-DD) in the given timezone from a UTC datetime."""
    local_time = utc_datetime.astimezone(tz)
    return local_time.strftime("%Y-%m-%d")


def _get_utc_midnight_for_calendar_day(calendar_day: str, tz: ZoneInfo) -> datetime:
    """Get UTC datetime for midnight of the calendar day in the given timezone."""
    local_midnight = datetime.strptime(calendar_day, "%Y-%m-%d").replace(
        tzinfo=tz, hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(timezone.utc)


class UsageService:
    """Service for aggregating and processing usage data"""

    def __init__(
        self,
        cost_service: CostBreakdownService,
    ):
        self.cost_service = cost_service

    async def aggregate_workspace_usage_for_date_range(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        return_records: bool = False,
    ) -> UsageMetrics | tuple[UsageMetrics, list[UsageRecordPydantic]]:
        """Aggregate workspace-level usage totals across multiple records for a date range."""
        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        total_cpu_seconds = sum(r.cpu_core_seconds for r in usage_records)
        total_memory_seconds = sum(r.memory_gb_seconds for r in usage_records)
        total_s3_hours = sum(r.s3_gb_hours for r in usage_records)
        total_efs_hours = sum(r.efs_gb_hours for r in usage_records)

        metrics = UsageMetrics(
            cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=total_s3_hours,
            efs_gb_hours=total_efs_hours,
        )

        if return_records:
            return metrics, usage_records
        return metrics

    def aggregate_deployment_usage_from_records(
        self,
        usage_records: list[UsageRecordPydantic],
        deployment_id: str,
    ) -> tuple[UsageMetrics, list[ServiceUsageItem], list[VolumeUsageItem]]:
        """Aggregate usage for a specific deployment across multiple usage records."""

        deployment_cpu_seconds = 0.0
        deployment_memory_seconds = 0.0
        deployment_s3_hours = 0.0
        deployment_efs_hours = 0.0
        aggregated_services: dict[str, ServiceUsageItem] = {}
        aggregated_volumes: dict[str, VolumeUsageItem] = {}

        for record in usage_records:
            for compute_breakdown in record.compute_breakdowns:
                if compute_breakdown.deployment_id == deployment_id:
                    deployment_cpu_seconds += compute_breakdown.cpu_core_seconds
                    deployment_memory_seconds += compute_breakdown.memory_gb_seconds

                    service_name = compute_breakdown.service_name
                    if service_name and service_name.strip():
                        service_name = service_name.strip()
                        if service_name not in aggregated_services:
                            aggregated_services[service_name] = ServiceUsageItem(
                                service_name=service_name,
                                cpu_core_seconds=0.0,
                                memory_gb_seconds=0.0,
                            )
                        aggregated_services[
                            service_name
                        ].cpu_core_seconds += compute_breakdown.cpu_core_seconds
                        aggregated_services[
                            service_name
                        ].memory_gb_seconds += compute_breakdown.memory_gb_seconds

            for storage_breakdown in record.storage_breakdowns:
                if storage_breakdown.deployment_id == deployment_id:
                    if storage_breakdown.storage_class == STORAGE_CLASS_S3:
                        deployment_s3_hours += storage_breakdown.gb_hours
                    elif storage_breakdown.storage_class == STORAGE_CLASS_EFS:
                        deployment_efs_hours += storage_breakdown.gb_hours

                    pvc_name = storage_breakdown.pvc_name
                    if pvc_name and pvc_name.strip():
                        volume_name = pvc_name.strip()
                        if volume_name not in aggregated_volumes:
                            aggregated_volumes[volume_name] = VolumeUsageItem(
                                volume_name=volume_name,
                                storage_class=storage_breakdown.storage_class,
                                gb_hours=0.0,
                            )
                        aggregated_volumes[
                            volume_name
                        ].gb_hours += storage_breakdown.gb_hours

        service_usage_list = list(aggregated_services.values())
        volume_usage_list = list(aggregated_volumes.values())

        metrics = UsageMetrics(
            cpu_core_hours=deployment_cpu_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=deployment_memory_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=deployment_s3_hours,
            efs_gb_hours=deployment_efs_hours,
        )

        return metrics, service_usage_list, volume_usage_list

    async def get_aggregated_usage_with_summaries(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> AggregatedUsageResponse:
        """Get aggregated usage across all user's workspaces with workspace summaries."""
        all_user_workspaces = (
            await db.workspaces.aget_user_workspaces_active_during_range(
                user_id=user_id,
                start_date=start_date,
                end_date=end_date,
            )
        )

        results: list[
            tuple[UsageMetrics | None, list[UsageRecordPydantic]]
        ] = await asyncio.gather(
            *[
                self.aggregate_workspace_usage_for_date_range(
                    str(workspace.id), start_date, end_date, return_records=True
                )
                for workspace, _ in all_user_workspaces
            ],
            return_exceptions=True,
        )

        workspace_usage_results: list[UsageMetrics | None] = []
        usage_records_list: list[list[UsageRecordPydantic]] = []
        record_counts: list[int] = []

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch usage: {result}", exc_info=True)
                workspace_usage_results.append(None)
                usage_records_list.append([])
                record_counts.append(0)
            else:
                metrics, records = result
                workspace_usage_results.append(metrics)
                usage_records_list.append(records)
                record_counts.append(len(records))

        cost_results = await self.cost_service.calculate_costs_batch(
            workspace_usage_results, external_customer_id
        )

        workspace_summaries, total_usage = await self._build_workspace_summaries(
            all_user_workspaces,
            workspace_usage_results,
            usage_records_list,
            record_counts,
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
                    s3_cost=sum(c.s3_cost for c in cost_results if c),
                    efs_cost=sum(c.efs_cost for c in cost_results if c),
                    total_cost=sum(c.total_cost for c in cost_results if c),
                )
            except Exception as e:
                logger.warning(f"Failed to calculate aggregated costs: {e}")

        total_usage.costs = total_costs

        return AggregatedUsageResponse(
            period=UsagePeriodInfo(start=start_date, end=end_date),
            usage=total_usage,
            workspace_count=len(all_user_workspaces),
            record_count=sum(record_counts),
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

        user_workspaces = await db.workspaces.aget_user_workspaces_active_during_range(
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )

        usage_records_tasks = [
            db.usage.get_workspace_usage(
                workspace_id=str(workspace.id),
                start_date=start_date,
                end_date=end_date,
                record_type=UsageCollectionConfig.get_record_type(),
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

            workspace_daily = self._aggregate_records_by_day(usage_records, tz)
            for day_key, day_data in workspace_daily.items():
                if day_key not in daily_data:
                    daily_data[day_key] = day_data
                else:
                    daily_data[day_key].cpu_core_hours += day_data.cpu_core_hours
                    daily_data[day_key].memory_gb_hours += day_data.memory_gb_hours
                    daily_data[day_key].s3_gb_hours += day_data.s3_gb_hours
                    daily_data[day_key].efs_gb_hours += day_data.efs_gb_hours

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
        usage_records: list[UsageRecordPydantic],
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> list[DeploymentUsageOverview]:
        """Build deployment overviews for a workspace with actual usage metrics."""
        deployments = await db.compose_deployments.afind_active_during_date_range(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            limit=100,
        )

        if not deployments:
            return []

        # Aggregate usage for all deployments and build a mapping
        deployment_metrics_map: dict[str, UsageMetrics] = {}
        valid_deployments: list = []
        for deployment in deployments:
            if not deployment.id or not deployment.name:
                continue

            valid_deployments.append(deployment)
            deployment_metrics, _, _ = self.aggregate_deployment_usage_from_records(
                usage_records=usage_records,
                deployment_id=str(deployment.id),
            )
            deployment_metrics_map[str(deployment.id)] = deployment_metrics

        # Calculate costs in parallel
        deployment_metrics_list = list(deployment_metrics_map.values())
        cost_results = await self.cost_service.calculate_costs_batch(
            usages=deployment_metrics_list,
            external_customer_id=external_customer_id,
        )

        # Build overviews with costs
        overviews: list[DeploymentUsageOverview] = []
        cost_idx = 0
        for deployment in valid_deployments:
            deployment_id = str(deployment.id)
            deployment_status = (
                "Active" if deployment.deleted_at is None else "Inactive"
            )

            metrics = deployment_metrics_map[deployment_id]

            # Attach costs if available
            if cost_idx < len(cost_results) and cost_results[cost_idx] is not None:
                metrics.costs = cost_results[cost_idx]
            cost_idx += 1

            overviews.append(
                DeploymentUsageOverview(
                    deployment_id=deployment_id,
                    deployment_name=deployment.name,
                    usage=metrics,
                    status=deployment_status,
                )
            )
        return overviews

    async def _build_workspace_summaries(
        self,
        workspaces: list,
        usages: list[UsageMetrics | None],
        usage_records_list: list[list[UsageRecordPydantic]],
        record_counts: list[int],
        cost_results: list[MeterCostBreakdown | None],
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> tuple[list[WorkspaceUsageSummary], UsageMetrics]:
        """Build workspace summaries and calculate totals."""
        workspace_summaries: list[WorkspaceUsageSummary] = []
        total_cpu_seconds = 0.0
        total_memory_seconds = 0.0
        total_s3_hours = 0.0
        total_efs_hours = 0.0
        total_records = 0

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
            usage_records = usage_records_list[i] if i < len(usage_records_list) else []
            deployment_overview_tasks.append(
                self._build_deployment_overviews(
                    str(workspace.id),
                    usage_records,
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
                    workspace_id=str(workspace.id),
                    workspace_name=workspace.name or "Unnamed Workspace",
                    workspace_status=workspace_status,
                    usage=usage,
                    record_count=record_counts[i],
                    deployments=deployment_overviews,
                )
            )

            total_cpu_seconds += usage.cpu_core_hours * SECONDS_PER_HOUR
            total_memory_seconds += usage.memory_gb_hours * SECONDS_PER_HOUR
            total_s3_hours += usage.s3_gb_hours
            total_efs_hours += usage.efs_gb_hours
            total_records += record_counts[i]

        total_usage = UsageMetrics(
            cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=total_s3_hours,
            efs_gb_hours=total_efs_hours,
        )

        return workspace_summaries, total_usage

    def _parse_timezone(self, timezone_str: str) -> ZoneInfo:
        """Parse timezone string, defaulting to UTC on error."""
        try:
            return ZoneInfo(timezone_str) if timezone_str else ZoneInfo("UTC")
        except Exception:
            logger.warning(f"Invalid timezone '{timezone_str}', defaulting to UTC")
            return ZoneInfo("UTC")

    def _aggregate_records_by_day(
        self,
        usage_records: list[UsageRecordPydantic],
        tz: ZoneInfo,
    ) -> dict[str, DailyUsageData]:
        """Aggregate usage records by calendar day in the given timezone."""
        daily_data: dict[str, DailyUsageData] = {}
        for record in usage_records:
            day_key = _get_calendar_day_in_timezone(record.collection_start, tz)
            if day_key not in daily_data:
                utc_midnight = _get_utc_midnight_for_calendar_day(day_key, tz)
                daily_data[day_key] = DailyUsageData(
                    date=utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    cpu_core_hours=0.0,
                    memory_gb_hours=0.0,
                    s3_gb_hours=0.0,
                    efs_gb_hours=0.0,
                )

            daily_data[day_key].cpu_core_hours += (
                record.cpu_core_seconds / SECONDS_PER_HOUR
            )
            daily_data[day_key].memory_gb_hours += (
                record.memory_gb_seconds / SECONDS_PER_HOUR
            )
            daily_data[day_key].s3_gb_hours += record.s3_gb_hours
            daily_data[day_key].efs_gb_hours += record.efs_gb_hours
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
                    s3_gb_hours=0.0,
                    efs_gb_hours=0.0,
                )
            current_local_date += timedelta(days=1)
