"""Service for calculating cost breakdowns from usage data."""

from loguru import logger
from pydantic import BaseModel

from shared.models.billing import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS
from shared.responses.usage import (
    MeterCostBreakdown,
    ServiceCostBreakdown,
    ServiceUsageItem,
    VolumeCostBreakdown,
    VolumeUsageItem,
)

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
        standard_gb_hours: float,
        shared_gb_hours: float,
        build_minutes: float,
        public_endpoint_hours: float,
        service_usage: list[ServiceUsageItem] | None = None,
        volume_usage: list[VolumeUsageItem] | None = None,
    ) -> WorkspaceCostBreakdown:
        """Calculate complete cost breakdown for workspace usage"""
        # Get meter prices from cache or Polar
        try:
            prices = await self.pricing.get_meter_prices(external_customer_id)

        except Exception as e:
            logger.error(f"Failed to get meter prices for cost calculation: {e}")
            raise ValueError(f"Cannot calculate costs: {e}")

        # Calculate meter-level costs
        meter_breakdown = self._calculate_meter_costs(
            prices=prices,
            cpu_core_hours=cpu_core_hours,
            memory_gb_hours=memory_gb_hours,
            standard_gb_hours=standard_gb_hours,
            shared_gb_hours=shared_gb_hours,
            build_minutes=build_minutes,
            public_endpoint_hours=public_endpoint_hours,
        )

        # Calculate service-level breakdown if provided
        service_breakdown = []
        if service_usage:
            service_breakdown = self._calculate_service_costs(
                prices=prices,
                service_usage=service_usage,
                total_cost=meter_breakdown.cpu_cost + meter_breakdown.memory_cost,
            )

        # Calculate volume-level breakdown if provided
        volume_breakdown = []
        if volume_usage:
            volume_breakdown = self._calculate_volume_costs(
                prices=prices,
                volume_usage=volume_usage,
                total_storage_cost=meter_breakdown.standard_cost
                + meter_breakdown.shared_cost,
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
        standard_gb_hours: float,
        shared_gb_hours: float,
        build_minutes: float,
        public_endpoint_hours: float,
    ) -> MeterCostBreakdown:
        """Calculate costs by meter type"""
        cpu_cost = cpu_core_hours * prices.cpu_price_per_unit
        memory_cost = memory_gb_hours * prices.memory_price_per_unit
        standard_cost = standard_gb_hours * prices.standard_price_per_unit
        shared_cost = shared_gb_hours * prices.shared_price_per_unit
        build_cost = build_minutes * prices.build_minutes_price_per_unit
        endpoint_cost = public_endpoint_hours * prices.endpoint_hours_price_per_unit
        total_cost = (
            cpu_cost
            + memory_cost
            + standard_cost
            + shared_cost
            + build_cost
            + endpoint_cost
        )

        return MeterCostBreakdown(
            cpu_cost=round(cpu_cost, 4),
            memory_cost=round(memory_cost, 4),
            standard_cost=round(standard_cost, 4),
            shared_cost=round(shared_cost, 4),
            build_cost=round(build_cost, 4),
            endpoint_cost=round(endpoint_cost, 4),
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
            cpu_core_hours = service.cpu_core_seconds / 3600.0
            memory_gb_hours = service.memory_gb_seconds / 3600.0

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

    def _calculate_volume_costs(
        self,
        prices: MeterPrices,
        volume_usage: list[VolumeUsageItem],
        total_storage_cost: float,
    ) -> list[VolumeCostBreakdown]:
        """Calculate costs per volume from storage usage breakdown"""
        if not volume_usage:
            return []

        breakdowns = []
        for volume in volume_usage:
            gb_hours = volume.gb_hours
            storage_class = volume.storage_class

            # Map storage class to meter price
            if storage_class == STORAGE_CLASS_EBS:
                storage_cost = gb_hours * prices.standard_price_per_unit
            elif storage_class == STORAGE_CLASS_EFS:
                storage_cost = gb_hours * prices.shared_price_per_unit
            else:
                logger.warning(
                    f"Unknown storage class '{storage_class}' for volume {volume.volume_name}, "
                    f"defaulting to EBS pricing"
                )
                storage_cost = gb_hours * prices.standard_price_per_unit

            percentage = (
                (storage_cost / total_storage_cost * 100)
                if total_storage_cost > 0
                else 0
            )

            breakdowns.append(
                VolumeCostBreakdown(
                    volume_name=volume.volume_name,
                    storage_class=storage_class,
                    storage_cost=round(storage_cost, 4),
                    percentage_of_total=round(percentage, 2),
                )
            )

        # Sort by cost descending
        breakdowns.sort(key=lambda x: x.storage_cost, reverse=True)

        return breakdowns
