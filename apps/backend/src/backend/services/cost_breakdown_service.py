import asyncio
from typing import Sequence

from loguru import logger
from responses.usage import DailyUsageData, MeterCostBreakdown, UsageMetrics

from backend.services.exceptions import NoActiveSubscriptionError
from backend.services.polar import PolarService

from .polar.cost_breakdown import WorkspaceCostBreakdown


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
        build_minutes: float,
        storage_gb_months: float,
        external_customer_id: str,
    ) -> MeterCostBreakdown:
        """Calculate costs from usage metrics without requiring database records"""
        if not self.polar_service.enabled:
            raise ValueError("Polar service is not enabled. Cannot calculate costs.")

        cost_breakdown: WorkspaceCostBreakdown = (
            await self.polar_service.cost_breakdown.calculate_workspace_costs(
                external_customer_id=external_customer_id,
                cpu_core_hours=cpu_core_hours,
                memory_gb_hours=memory_gb_hours,
                build_minutes=build_minutes,
                storage_gb_months=storage_gb_months,
            )
        )

        return cost_breakdown.meter_breakdown

    async def calculate_costs_batch(
        self,
        usages: Sequence[UsageMetrics | None],
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
                build_minutes=usage.build_minutes,
                storage_gb_months=usage.storage_gb_months,
                external_customer_id=external_customer_id,
            )
            for usage in usages
            if usage is not None
        ]
        cost_results_raw = await asyncio.gather(*cost_tasks, return_exceptions=True)
        cost_results: list[MeterCostBreakdown | None] = []
        for result in cost_results_raw:
            if isinstance(result, NoActiveSubscriptionError):
                # Expected case - user has no subscription, skip silently
                cost_results.append(None)
            elif isinstance(result, BaseException):
                logger.warning(f"Failed to calculate costs: {result}")
                cost_results.append(None)
            else:
                cost_results.append(result)

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
                build_minutes=day_data.build_minutes,
                storage_gb_months=day_data.storage_gb_months,
                external_customer_id=external_customer_id,
            )
            for day_data in daily_data.values()
        ]

        cost_results = await asyncio.gather(*cost_tasks, return_exceptions=True)

        # Check if first result is NoActiveSubscriptionError - if so, skip all silently
        # (all days will fail the same way for the same customer)
        if cost_results and isinstance(cost_results[0], NoActiveSubscriptionError):
            # Expected case - user has no subscription, leave costs as None
            return

        for (day_key, day_data), cost_result in zip(daily_data.items(), cost_results):
            if isinstance(cost_result, NoActiveSubscriptionError):
                # Expected case - user has no subscription, skip silently
                pass
            elif isinstance(cost_result, BaseException):
                logger.warning(
                    f"Failed to calculate costs for {day_key}: {cost_result}"
                )
            else:
                day_data.costs = cost_result

    def is_enabled(self) -> bool:
        """Check if cost calculation is enabled (Polar service is configured)."""
        return self.polar_service.enabled
