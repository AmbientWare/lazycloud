from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services import platform_manager
from src.services.platform.platform_manager import (
    PricingData,
    Markups,
    GPUPricingTable,
    UnitPricing,
)
from src.config import app_config


pricing_router = APIRouter(prefix="/pricing", tags=["pricing"])


@pricing_router.get("/")
async def get_pricing() -> PricingData:
    """Get all pricing data including region markups, compute pricing, and GPU pricing."""
    return await platform_manager.get_latest_pricing_data()


@pricing_router.get("/markups")
async def get_region_markups() -> Markups:
    """Get region markup data."""
    pricing_data = await platform_manager.get_latest_pricing_data()
    if not pricing_data.markups:
        raise HTTPException(status_code=404, detail="Region markup data not found")
    return pricing_data.markups


@pricing_router.get("/compute")
async def get_compute_pricing() -> UnitPricing:
    try:
        return await platform_manager.get_unit_pricing()
    except ValueError as e:
        raise HTTPException(
            status_code=404, detail=f"Unit pricing data not available: {str(e)}"
        )


@pricing_router.get("/gpu")
async def get_gpu_pricing() -> GPUPricingTable:
    """Get GPU pricing data."""
    pricing_data = await platform_manager.get_latest_pricing_data()
    if not pricing_data.gpu_pricing:
        raise HTTPException(status_code=404, detail="GPU pricing data not found")
    return pricing_data.gpu_pricing


class PriceResponse(BaseModel):
    price: float
    units: str


@pricing_router.get("/volume")
async def get_volume_pricing() -> PriceResponse:
    """Get volume storage pricing."""
    return PriceResponse(price=app_config.VOLUME_PRICE, units="GB/month")


@pricing_router.get("/ipv4")
async def get_dedicated_ipv4_pricing() -> PriceResponse:
    """Get dedicated IPv4 pricing."""
    return PriceResponse(price=app_config.DEDICATED_IPV4_PRICE, units="month")


@pricing_router.get("/egress")
async def get_data_egress_pricing() -> PriceResponse:
    """Get data egress pricing."""
    return PriceResponse(price=app_config.DATA_EGRESS_PRICE, units="GB")
