from dataclasses import dataclass

from models.billing import MeterNames
from polar_sdk.models import SubscriptionRecurringInterval, UnitAmount
from pydantic import BaseModel


@dataclass
class MeterPrice:
    """Metered price configuration."""

    meter_name: MeterNames
    unit_amount: UnitAmount  # amount in cents, up to 12 decimal places


METER_PRICE_MAP = {
    MeterNames.CPU_USAGE: 4.0,  # $0.04 per CPU core hour
    MeterNames.MEMORY_USAGE: 0.8,  # $0.008 per memory GB hour
    MeterNames.STANDARD_STORAGE: 0.015,  # $0.00015 per standard storage GB hour (~$0.11/GB-month)
    MeterNames.SHARED_STORAGE: 0.06,  # $0.0006 per shared (EFS) storage GB hour (~$0.43/GB-month)
    MeterNames.BUILD_MINUTES: 4.0,  # $0.04 per build minute (matches Depot overage)
    MeterNames.PUBLIC_ENDPOINTS: 0.07,  # $0.007 per endpoint-hour (~$0.50/month, 400% markup over Cloudflare)
}

METER_PRICES = [
    MeterPrice(meter_name=meter_name, unit_amount=METER_PRICE_MAP[meter_name])
    for meter_name in MeterNames
]


class ProductMetadata(BaseModel):
    """Product metadata for LazyCloud billing"""

    tier: str
    features: str  # JSON string


@dataclass
class ProductDefinition:
    """Complete product definition for LazyCloud billing."""

    name: str
    description: str
    recurring_interval: SubscriptionRecurringInterval
    has_free_base: bool
    meter_prices: list[MeterPrice]
    metadata: ProductMetadata
    monthly_fee: int | None
    recurring_interval_count: int = 1
