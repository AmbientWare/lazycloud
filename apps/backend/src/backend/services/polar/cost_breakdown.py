"""Service for calculating cost breakdowns from usage data."""

from loguru import logger
from models.billing import UsageUnits
from pydantic import BaseModel
from responses.usage import (
    MeterCostBreakdown,
    ServiceCostBreakdown,
    ServiceUsageItem,
    VolumeCostBreakdown,
    VolumeUsageItem,
)

from backend.services.exceptions import NoActiveSubscriptionError

from .pricing import MeterPrices, PolarPricingModule


class WorkspaceCostBreakdown(BaseModel):
    """Complete cost breakdown for workspace usage."""

    meter_breakdown: MeterCostBreakdown
    service_breakdown: list[ServiceCostBreakdown]
    volume_breakdown: list[VolumeCostBreakdown]
    is_estimated: bool = (
        True  # for now just true since prices are calculated not direct
    )


class PolarCostBreakdownModule:
    """Service for calculating cost breakdowns from usage data."""

    def __init__(self, pricing_module: PolarPricingModule):
        """Initialize the cost breakdown module."""
        self.pricing = pricing_module

    async def calculate_workspace_costs(
        self,
        external_customer_id: str,
        cpu_core_hours: float,
        memory_gb_hours: float,
        build_minutes: float,
        storage_gb_months: float = 0.0,
        service_usage: list[ServiceUsageItem] | None = None,
        volume_usage: list[VolumeUsageItem] | None = None,
    ) -> WorkspaceCostBreakdown:
        """Calculate complete cost breakdown for workspace usage."""
        # Get meter prices from cache or Polar
        try:
            prices = await self.pricing.get_meter_prices(external_customer_id)

        except NoActiveSubscriptionError:
            # Expected case - re-raise without logging as error
            raise

        except Exception as e:
            logger.error(f"Failed to get meter prices for cost calculation: {e}")
            raise ValueError(f"Cannot calculate costs: {e}")

        # Calculate meter-level costs
        meter_breakdown = self._calculate_meter_costs(
            prices=prices,
            cpu_core_hours=cpu_core_hours,
            memory_gb_hours=memory_gb_hours,
            build_minutes=build_minutes,
            storage_gb_months=storage_gb_months,
        )

        # Calculate service-level breakdown if provided
        service_breakdown = []
        if service_usage:
            service_breakdown = self._calculate_service_costs(
                prices=prices,
                service_usage=service_usage,
                total_cost=meter_breakdown.cpu_cost + meter_breakdown.memory_cost,
            )

        # Volume breakdown with actual storage costs
        volume_breakdown = []
        if volume_usage:
            total_volume_gb_hours = sum(v.gb_hours for v in volume_usage)
            for v in volume_usage:
                # Convert gb_hours to gb_months and calculate cost
                v_gb_months = UsageUnits.gb_hours_to_gb_months(v.gb_hours)
                v_cost = v_gb_months * prices.storage_price_per_unit
                v_pct = (
                    (v.gb_hours / total_volume_gb_hours * 100)
                    if total_volume_gb_hours > 0
                    else 0
                )
                volume_breakdown.append(
                    VolumeCostBreakdown(
                        volume_name=v.volume_name,
                        storage_class=v.storage_class,
                        storage_cost=round(v_cost, 4),
                        percentage_of_total=round(v_pct, 2),
                    )
                )

        return WorkspaceCostBreakdown(
            meter_breakdown=meter_breakdown,
            service_breakdown=service_breakdown,
            volume_breakdown=volume_breakdown,
            is_estimated=True,
        )

    def _calculate_meter_costs(
        self,
        prices: MeterPrices,
        cpu_core_hours: float,
        memory_gb_hours: float,
        build_minutes: float,
        storage_gb_months: float = 0.0,
    ) -> MeterCostBreakdown:
        """Calculate costs by meter type"""
        cpu_cost = cpu_core_hours * prices.cpu_price_per_unit
        memory_cost = memory_gb_hours * prices.memory_price_per_unit
        build_cost = build_minutes * prices.build_minutes_price_per_unit
        storage_cost = storage_gb_months * prices.storage_price_per_unit
        total_cost = cpu_cost + memory_cost + build_cost + storage_cost

        return MeterCostBreakdown(
            cpu_cost=round(cpu_cost, 4),
            memory_cost=round(memory_cost, 4),
            build_cost=round(build_cost, 4),
            storage_cost=round(storage_cost, 4),
            total_cost=round(total_cost, 4),
        )

    def _calculate_service_costs(
        self,
        prices: MeterPrices,
        service_usage: list[ServiceUsageItem],
        total_cost: float,
    ) -> list[ServiceCostBreakdown]:
        """Calculate costs per service from compute usage breakdown"""
        if not service_usage:
            return []

        breakdowns = []
        for service in service_usage:
            # Convert seconds to hours
            cpu_core_hours = UsageUnits.seconds_to_hours(service.cpu_core_seconds)
            memory_gb_hours = UsageUnits.seconds_to_hours(service.memory_gb_seconds)

            cpu_cost = cpu_core_hours * prices.cpu_price_per_unit
            memory_cost = memory_gb_hours * prices.memory_price_per_unit
            service_total = cpu_cost + memory_cost

            percentage = (service_total / total_cost * 100) if total_cost > 0 else 0

            breakdowns.append(
                ServiceCostBreakdown(
                    service_name=service.service_name,
                    cpu_core_hours=round(cpu_core_hours, 2),
                    memory_gb_hours=round(memory_gb_hours, 2),
                    cpu_cost=round(cpu_cost, 4),
                    memory_cost=round(memory_cost, 4),
                    total_compute_cost=round(service_total, 4),
                    percentage_of_total=round(percentage, 2),
                )
            )

        # Sort by total cost descending
        breakdowns.sort(key=lambda x: x.total_compute_cost, reverse=True)

        return breakdowns
