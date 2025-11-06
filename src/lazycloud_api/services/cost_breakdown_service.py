from datetime import datetime

from fastapi import HTTPException

from lazycloud_api.database import db
from lazycloud_api.services.polar import PolarService
from lazycloud_api.services.usage_service import UsageService
from shared.models.billing import SECONDS_PER_HOUR, UsageCollectionConfig
from shared.responses.usage import (
    MeterCostBreakdown,
    ServiceCostBreakdown,
    UsagePeriodInfo,
    VolumeCostBreakdown,
    WorkspaceCostBreakdownResponse,
)


class CostBreakdownService:
    """Service for calculating and retrieving workspace cost breakdowns."""

    def __init__(
        self,
        polar_service: PolarService,
        usage_service: UsageService,
    ):
        self.polar_service = polar_service
        self.usage_service = usage_service

    async def get_deployment_cost_breakdown(
        self,
        workspace_id: str,
        deployment_id: str,
        external_customer_id: str,
    ) -> WorkspaceCostBreakdownResponse:
        """Get detailed cost breakdown for a specific deployment"""
        polar_service = self.polar_service
        usage_service = self.usage_service

        # Verify deployment exists and belongs to workspace
        deployment = await db.compose_deployments.aget_by_id(
            deployment_id, include_deleted=True
        )
        if not deployment or deployment.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Get usage breakdown
        (
            metrics,
            services,
            volumes,
            usage_record,
        ) = await usage_service.get_workspace_usage_breakdown(
            workspace_id=workspace_id,
            deployment_id=deployment_id,
        )

        if not usage_record:
            raise HTTPException(status_code=404, detail="Usage record not found")

        # Calculate costs
        cost_breakdown = await polar_service.cost_breakdown.calculate_workspace_costs(
            external_customer_id=external_customer_id,
            cpu_core_hours=metrics.cpu_core_hours,
            memory_gb_hours=metrics.memory_gb_hours,
            s3_gb_hours=metrics.s3_gb_hours,
            efs_gb_hours=metrics.efs_gb_hours,
            service_usage=services,
            volume_usage=volumes,
        )

        return WorkspaceCostBreakdownResponse(
            workspace_id=workspace_id,
            period=UsagePeriodInfo(
                start=usage_record.collection_start,
                end=usage_record.collection_end,
            ),
            meter_breakdown=MeterCostBreakdown(
                cpu_cost=cost_breakdown.meter_breakdown.cpu_cost,
                memory_cost=cost_breakdown.meter_breakdown.memory_cost,
                s3_cost=cost_breakdown.meter_breakdown.s3_cost,
                efs_cost=cost_breakdown.meter_breakdown.efs_cost,
                total_cost=cost_breakdown.meter_breakdown.total_cost,
            ),
            service_breakdown=[
                ServiceCostBreakdown(
                    service_name=s.service_name,
                    cpu_core_hours=s.cpu_core_hours,
                    memory_gb_hours=s.memory_gb_hours,
                    cpu_cost=s.cpu_cost,
                    memory_cost=s.memory_cost,
                    total_compute_cost=s.total_compute_cost,
                    percentage_of_total=s.percentage_of_total,
                )
                for s in cost_breakdown.service_breakdown
            ],
            volume_breakdown=[
                VolumeCostBreakdown(
                    volume_name=v.volume_name,
                    storage_class=v.storage_class,
                    storage_cost=v.storage_cost,
                    percentage_of_total=v.percentage_of_total,
                )
                for v in cost_breakdown.volume_breakdown
            ],
            is_estimated=True,
        )

    async def get_aggregated_cost_breakdown(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        external_customer_id: str,
    ) -> WorkspaceCostBreakdownResponse:
        """Get aggregated cost breakdown for a date range"""
        polar_service = self.polar_service

        # Get all usage records in date range
        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            record_type=UsageCollectionConfig.get_record_type(),
        )

        # Aggregate usage
        total_cpu_seconds = sum(r.cpu_core_seconds for r in usage_records)
        total_memory_seconds = sum(r.memory_gb_seconds for r in usage_records)
        total_s3_hours = sum(r.s3_gb_hours for r in usage_records)
        total_efs_hours = sum(r.efs_gb_hours for r in usage_records)

        # Calculate costs
        cost_breakdown = await polar_service.cost_breakdown.calculate_workspace_costs(
            external_customer_id=external_customer_id,
            cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=total_s3_hours,
            efs_gb_hours=total_efs_hours,
        )

        return WorkspaceCostBreakdownResponse(
            workspace_id=workspace_id,
            period=UsagePeriodInfo(start=start_date, end=end_date),
            meter_breakdown=MeterCostBreakdown(
                cpu_cost=cost_breakdown.meter_breakdown.cpu_cost,
                memory_cost=cost_breakdown.meter_breakdown.memory_cost,
                s3_cost=cost_breakdown.meter_breakdown.s3_cost,
                efs_cost=cost_breakdown.meter_breakdown.efs_cost,
                total_cost=cost_breakdown.meter_breakdown.total_cost,
            ),
            service_breakdown=[],
            volume_breakdown=[],
            is_estimated=True,
        )

    async def calculate_costs_from_usage(
        self,
        cpu_core_hours: float,
        memory_gb_hours: float,
        s3_gb_hours: float,
        efs_gb_hours: float,
        external_customer_id: str,
    ) -> MeterCostBreakdown:
        """Calculate costs from usage metrics without requiring database records"""
        cost_breakdown = (
            await self.polar_service.cost_breakdown.calculate_workspace_costs(
                external_customer_id=external_customer_id,
                cpu_core_hours=cpu_core_hours,
                memory_gb_hours=memory_gb_hours,
                s3_gb_hours=s3_gb_hours,
                efs_gb_hours=efs_gb_hours,
            )
        )

        return MeterCostBreakdown(
            cpu_cost=cost_breakdown.meter_breakdown.cpu_cost,
            memory_cost=cost_breakdown.meter_breakdown.memory_cost,
            s3_cost=cost_breakdown.meter_breakdown.s3_cost,
            efs_cost=cost_breakdown.meter_breakdown.efs_cost,
            total_cost=cost_breakdown.meter_breakdown.total_cost,
        )
