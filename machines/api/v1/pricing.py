from fastapi import APIRouter

from machines.services import pricing_manager
from machines.services.pricing_manager import PricingData, Markups, PricingTable

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
