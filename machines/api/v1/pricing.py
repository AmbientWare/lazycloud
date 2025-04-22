from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from machines.services import platform_manager
from machines.services.platform.platform_manager import (
    PricingData,
    Markups,
    PricingTable,
    GPUPricingTable,
)
from machines.config import app_config


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
async def get_compute_pricing() -> PricingTable:
    """Get compute instance pricing data."""
    pricing_data = await platform_manager.get_latest_pricing_data()
    if not pricing_data.pricing_table:
        raise HTTPException(status_code=404, detail="Compute pricing data not found")
    return pricing_data.pricing_table


@pricing_router.get("/gpu")
async def get_gpu_pricing() -> GPUPricingTable:
    """Get GPU pricing data."""
    pricing_data = await platform_manager.get_latest_pricing_data()
    if not pricing_data.gpu_pricing:
        raise HTTPException(status_code=404, detail="GPU pricing data not found")
    return pricing_data.gpu_pricing


class PriceResponse(BaseModel):
    price: str


@pricing_router.get("/volume")
async def get_volume_pricing() -> PriceResponse:
    """Get volume storage pricing."""
    return PriceResponse(price=f"${app_config.VOLUME_PRICE}/GB")


@pricing_router.get("/ipv4")
async def get_dedicated_ipv4_pricing() -> PriceResponse:
    """Get dedicated IPv4 pricing."""
    return PriceResponse(price=f"${app_config.DEDICATED_IPV4_PRICE}")


@pricing_router.get("/egress")
async def get_data_egress_pricing() -> PriceResponse:
    """Get data egress pricing."""
    return PriceResponse(price=f"${app_config.DATA_EGRESS_PRICE}/GB")
