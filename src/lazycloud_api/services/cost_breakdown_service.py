import asyncio

from loguru import logger

from lazycloud_api.services.polar import PolarService
from shared.responses.usage import DailyUsageData, MeterCostBreakdown, UsageMetrics


class CostBreakdownService:
    """Service for calculating and retrieving workspace cost breakdowns"""

    def __init__(
        self,
        polar_service: PolarService,
    ):
        self.polar_service = polar_service

    async def calculate_costs_from_usage(
        self,
        cpu_core_hours: float,
        memory_gb_hours: float,
        s3_gb_hours: float,
        efs_gb_hours: float,
        build_minutes: float,
        public_endpoint_hours: float,
        external_customer_id: str,
    ) -> MeterCostBreakdown:
        """Calculate costs from usage metrics without requiring database records"""
        if not self.polar_service.enabled:
            raise ValueError("Polar service is not enabled. Cannot calculate costs.")

        cost_breakdown = (
            await self.polar_service.cost_breakdown.calculate_workspace_costs(
                external_customer_id=external_customer_id,
                cpu_core_hours=cpu_core_hours,
                memory_gb_hours=memory_gb_hours,
                s3_gb_hours=s3_gb_hours,
                efs_gb_hours=efs_gb_hours,
                build_minutes=build_minutes,
                public_endpoint_hours=public_endpoint_hours,
            )
        )

        return MeterCostBreakdown(
            cpu_cost=cost_breakdown.meter_breakdown.cpu_cost,
            memory_cost=cost_breakdown.meter_breakdown.memory_cost,
            s3_cost=cost_breakdown.meter_breakdown.s3_cost,
            efs_cost=cost_breakdown.meter_breakdown.efs_cost,
            build_cost=cost_breakdown.meter_breakdown.build_cost,
            endpoint_cost=cost_breakdown.meter_breakdown.endpoint_cost,
            total_cost=cost_breakdown.meter_breakdown.total_cost,
        )

    async def calculate_costs_batch(
        self,
        usages: list[UsageMetrics | None],
        external_customer_id: str,
    ) -> list[MeterCostBreakdown | None]:
        """Calculate costs for multiple usage metrics in parallel.

        Returns a list of cost breakdowns, with None for any that failed to calculate.
        """
        if not self.polar_service.enabled:
            return []

        cost_tasks = [
            self.calculate_costs_from_usage(
                cpu_core_hours=usage.cpu_core_hours,
                memory_gb_hours=usage.memory_gb_hours,
                s3_gb_hours=usage.s3_gb_hours,
                efs_gb_hours=usage.efs_gb_hours,
                build_minutes=usage.build_minutes,
                public_endpoint_hours=usage.public_endpoint_hours,
                external_customer_id=external_customer_id,
            )
            for usage in usages
            if usage is not None
        ]
        cost_results_raw = await asyncio.gather(*cost_tasks, return_exceptions=True)
        cost_results = [
            r if not isinstance(r, Exception) else None for r in cost_results_raw
        ]
        for result in cost_results_raw:
            if isinstance(result, Exception):
                logger.warning(f"Failed to calculate costs: {result}", exc_info=True)

        return cost_results

    async def calculate_costs_for_daily_data(
        self,
        daily_data: dict[str, DailyUsageData],
        external_customer_id: str,
    ) -> None:
        """Calculate costs for each day's usage data and attach to the data structures."""
        if not self.polar_service.enabled:
            return

        cost_tasks = [
            self.calculate_costs_from_usage(
                cpu_core_hours=day_data.cpu_core_hours,
                memory_gb_hours=day_data.memory_gb_hours,
                s3_gb_hours=day_data.s3_gb_hours,
                efs_gb_hours=day_data.efs_gb_hours,
                build_minutes=day_data.build_minutes,
                public_endpoint_hours=day_data.public_endpoint_hours,
                external_customer_id=external_customer_id,
            )
            for day_data in daily_data.values()
        ]

        cost_results = await asyncio.gather(*cost_tasks, return_exceptions=True)

        for (day_key, day_data), cost_result in zip(daily_data.items(), cost_results):
            if isinstance(cost_result, Exception):
                logger.warning(
                    f"Failed to calculate costs for {day_key}: {cost_result}",
                    exc_info=True,
                )
            else:
                day_data.costs = cost_result

    def is_enabled(self) -> bool:
        """Check if cost calculation is enabled (Polar service is configured)."""
        return self.polar_service.enabled
