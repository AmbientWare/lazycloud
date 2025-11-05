"""Service for calculating cost breakdowns from usage data."""

from loguru import logger
from pydantic import BaseModel

from shared.models.billing import STORAGE_CLASS_EFS, STORAGE_CLASS_S3
from shared.responses.usage import ServiceUsageItem, VolumeUsageItem

from .pricing import MeterPrices, PolarPricingModule


class MeterCostBreakdown(BaseModel):
    """Cost breakdown by meter type."""

    cpu_cost: float
    memory_cost: float
    s3_cost: float
    efs_cost: float
    total_cost: float


class ServiceCostBreakdown(BaseModel):
    """Cost breakdown for a single service."""

    service_name: str
    cpu_cost: float
    memory_cost: float
    total_compute_cost: float
    percentage_of_total: float


class VolumeCostBreakdown(BaseModel):
    """Cost breakdown for a single volume."""

    volume_name: str
    storage_class: str
    storage_cost: float
    percentage_of_total: float


class WorkspaceCostBreakdown(BaseModel):
    """Complete cost breakdown for workspace usage."""

    meter_breakdown: MeterCostBreakdown
    service_breakdown: list[ServiceCostBreakdown]
    volume_breakdown: list[VolumeCostBreakdown]
    is_estimated: bool = True  # Always true since we calculate from prices


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
        s3_gb_hours: float,
        efs_gb_hours: float,
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
            s3_gb_hours=s3_gb_hours,
            efs_gb_hours=efs_gb_hours,
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
                total_storage_cost=meter_breakdown.s3_cost + meter_breakdown.efs_cost,
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
        s3_gb_hours: float,
        efs_gb_hours: float,
    ) -> MeterCostBreakdown:
        """Calculate costs by meter type"""
        cpu_cost = cpu_core_hours * prices.cpu_price_per_unit
        memory_cost = memory_gb_hours * prices.memory_price_per_unit
        s3_cost = s3_gb_hours * prices.s3_price_per_unit
        efs_cost = efs_gb_hours * prices.efs_price_per_unit
        total_cost = cpu_cost + memory_cost + s3_cost + efs_cost

        logger.debug(
            f"Calculated meter costs: CPU=${cpu_cost:.4f}, "
            f"Memory=${memory_cost:.4f}, S3=${s3_cost:.4f}, EFS=${efs_cost:.4f}, "
            f"Total=${total_cost:.4f}"
        )

        return MeterCostBreakdown(
            cpu_cost=round(cpu_cost, 4),
            memory_cost=round(memory_cost, 4),
            s3_cost=round(s3_cost, 4),
            efs_cost=round(efs_cost, 4),
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
                    cpu_cost=round(cpu_cost, 4),
                    memory_cost=round(memory_cost, 4),
                    total_compute_cost=round(service_total, 4),
                    percentage_of_total=round(percentage, 2),
                )
            )

        # Sort by total cost descending
        breakdowns.sort(key=lambda x: x.total_compute_cost, reverse=True)

        logger.debug(
            f"Calculated costs for {len(breakdowns)} services, "
            f"top service: {breakdowns[0].service_name if breakdowns else 'none'} "
            f"(${breakdowns[0].total_compute_cost:.2f})"
            if breakdowns
            else ""
        )

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
            if storage_class == STORAGE_CLASS_S3:
                storage_cost = gb_hours * prices.s3_price_per_unit
            elif storage_class == STORAGE_CLASS_EFS:
                storage_cost = gb_hours * prices.efs_price_per_unit
            else:
                logger.warning(
                    f"Unknown storage class '{storage_class}' for volume {volume.volume_name}, "
                    f"defaulting to S3 pricing"
                )
                storage_cost = gb_hours * prices.s3_price_per_unit

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

        logger.debug(
            f"Calculated costs for {len(breakdowns)} volumes, "
            f"top volume: {breakdowns[0].volume_name if breakdowns else 'none'} "
            f"(${breakdowns[0].storage_cost:.2f})"
            if breakdowns
            else ""
        )

        return breakdowns
