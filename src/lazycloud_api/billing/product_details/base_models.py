from dataclasses import dataclass

from polar_sdk.models import SubscriptionRecurringInterval, UnitAmount
from pydantic import BaseModel

from shared.models.billing import MeterNames


@dataclass
class MeterPrice:
    """Metered price configuration."""

    meter_name: MeterNames
    unit_amount: UnitAmount  # amount in cents, up to 12 decimal places


METER_PRICE_MAP = {
    MeterNames.CPU_USAGE: 1.0,
    MeterNames.MEMORY_USAGE: 1.0,
    MeterNames.STANDARD_STORAGE: 1.0,
    MeterNames.PREMIUM_STORAGE: 1.0,
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
