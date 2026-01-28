"""Billing and pricing API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from models.billing import METER_PRICES_CENTS, MeterNames
from pydantic import BaseModel
from responses.billing import BillingCycleResponse

from backend.api.security import get_current_active_user
from backend.database.users import UserPydantic
from backend.services import get_polar_service

billing_router = APIRouter(prefix="/billing", tags=["billing"])


class MeterPricingResponse(BaseModel):
    """Response model for meter pricing information.

    All prices are in cents per unit.
    """

    cpu_usage: float  # cents per core-hour
    memory_usage: float  # cents per GB-hour
    build_minutes: float  # cents per minute
    storage_usage: float  # cents per GB-month


@billing_router.get("/meter-pricing", response_model=MeterPricingResponse)
async def get_meter_pricing() -> MeterPricingResponse:
    """Get current meter pricing rates."""
    return MeterPricingResponse(
        cpu_usage=METER_PRICES_CENTS[MeterNames.CPU_USAGE],
        memory_usage=METER_PRICES_CENTS[MeterNames.MEMORY_USAGE],
        build_minutes=METER_PRICES_CENTS[MeterNames.BUILD_MINUTES],
        storage_usage=METER_PRICES_CENTS[MeterNames.STORAGE_USAGE],
    )


@billing_router.get("/cycle", response_model=BillingCycleResponse)
async def get_billing_cycle(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> BillingCycleResponse:
    """Get the current billing cycle dates for the authenticated user."""
    polar_service = get_polar_service()

    try:
        return await polar_service.subscriptions.get_billing_cycle(
            external_customer_id=current_user.workos_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
