from fastapi import APIRouter
from pydantic import BaseModel
from machines.services import pricing_manager
from machines.services.pricing_manager import PricingData, Markups, PricingTable
from machines.config import app_config


pricing_router = APIRouter(prefix="/pricing", tags=["pricing"])


@pricing_router.get("")
async def get_pricing() -> PricingData:
    return await pricing_manager.get_latest_pricing_data()


@pricing_router.get("/markups")
async def get_region_markups() -> Markups:
    return await pricing_manager.get_region_markups_from_redis()


@pricing_router.get("/pricing-table")
async def get_pricing_table() -> PricingTable:
    return await pricing_manager.get_pricing_table_from_redis()


class PriceResponse(BaseModel):
    price: str


@pricing_router.get("/volume")
async def get_volume_pricing() -> PriceResponse:
    return PriceResponse(price=f"${app_config.VOLUME_PRICE}/GB")


@pricing_router.get("/ipv4")
async def get_dedicated_ipv4_pricing() -> PriceResponse:
    return PriceResponse(price=f"${app_config.DEDICATED_IPV4_PRICE}")


@pricing_router.get("/egress")
async def get_data_egress_pricing() -> PriceResponse:
    return PriceResponse(price=f"${app_config.DATA_EGRESS_PRICE}/GB")
