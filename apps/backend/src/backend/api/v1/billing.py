"""Billing and pricing API endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.billing.product_details.base_models import METER_PRICE_MAP
from models.billing import MeterNames

billing_router = APIRouter(prefix="/billing", tags=["billing"])


class MeterPricingResponse(BaseModel):
    """Response model for meter pricing information.

    All prices are in cents per unit-hour (or per minute for build minutes).
    """

    cpu_usage: float  # cents per core-hour
    memory_usage: float  # cents per GB-hour
    standard_storage: float  # cents per GB-hour
    shared_storage: float  # cents per GB-hour
    build_minutes: float  # cents per minute
    public_endpoints: float  # cents per endpoint-hour


@billing_router.get("/meter-pricing", response_model=MeterPricingResponse)
async def get_meter_pricing() -> MeterPricingResponse:
    """Get current meter pricing rates."""
    return MeterPricingResponse(
        cpu_usage=METER_PRICE_MAP[MeterNames.CPU_USAGE],
        memory_usage=METER_PRICE_MAP[MeterNames.MEMORY_USAGE],
        standard_storage=METER_PRICE_MAP[MeterNames.STANDARD_STORAGE],
        shared_storage=METER_PRICE_MAP[MeterNames.SHARED_STORAGE],
        build_minutes=METER_PRICE_MAP[MeterNames.BUILD_MINUTES],
        public_endpoints=METER_PRICE_MAP[MeterNames.PUBLIC_ENDPOINTS],
    )
