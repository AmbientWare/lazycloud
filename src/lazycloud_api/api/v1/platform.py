from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lazycloud_api.services import platform_manager
from lazycloud_api.services.platform.platform_manager import (
    PricingData,
    Markups,
    GPUPricingTable,
    UnitPricing,
    PlatformOptions,
)
from lazycloud_api.config import app_config


platform_router = APIRouter(prefix="/platform", tags=["platform"])


@platform_router.get("/options")
async def get_platform_options() -> PlatformOptions:
    return await platform_manager.get_platform_options()


@platform_router.get("/pricing")
async def get_pricing() -> PricingData:
    """Get all pricing data including region markups, compute pricing, and GPU pricing."""
    return await platform_manager.get_latest_pricing_data()


@platform_router.get("/pricing/markups")
async def get_region_markups() -> Markups:
    """Get region markup data."""
    pricing_data = await platform_manager.get_latest_pricing_data()
    if not pricing_data.markups:
        raise HTTPException(status_code=404, detail="Region markup data not found")
    return pricing_data.markups


@platform_router.get("/pricing/compute")
async def get_compute_pricing() -> UnitPricing:
    try:
        return await platform_manager.get_unit_pricing()
    except ValueError as e:
        raise HTTPException(
            status_code=404, detail=f"Unit pricing data not available: {str(e)}"
        )


@platform_router.get("/pricing/gpu")
async def get_gpu_pricing() -> GPUPricingTable:
    """Get GPU pricing data."""
    pricing_data = await platform_manager.get_latest_pricing_data()
    if not pricing_data.gpu_pricing:
        raise HTTPException(status_code=404, detail="GPU pricing data not found")
    return pricing_data.gpu_pricing


class PriceResponse(BaseModel):
    price: float
    units: str


@platform_router.get("/pricing/volume")
async def get_volume_pricing() -> PriceResponse:
    """Get volume storage pricing."""
    return PriceResponse(price=app_config.VOLUME_PRICE, units="GB/month")


@platform_router.get("/pricing/ipv4")
async def get_dedicated_ipv4_pricing() -> PriceResponse:
    """Get dedicated IPv4 pricing."""
    return PriceResponse(price=app_config.DEDICATED_IPV4_PRICE, units="month")


@platform_router.get("/pricing/egress")
async def get_data_egress_pricing() -> PriceResponse:
    """Get data egress pricing."""
    return PriceResponse(price=app_config.DATA_EGRESS_PRICE, units="GB")
