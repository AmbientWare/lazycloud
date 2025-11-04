from dataclasses import dataclass

from loguru import logger
from polar_sdk.models import (
    ProductCreateRecurringPrices,
    ProductPriceFixedCreate,
    ProductPriceFreeCreate,
    ProductPriceMeteredUnitCreate,
    SubscriptionRecurringInterval,
    UnitAmount,
)

from lazycloud_api.services import get_polar_service
from shared.models.billing import MeterNames


@dataclass
class MeterPrice:
    """Metered price configuration."""

    meter_name: MeterNames
    unit_amount: UnitAmount  # amount in cents, up to 12 decimal places


@dataclass
class ProductDefinition:
    """Complete product definition for LazyCloud billing."""

    name: str
    description: str
    recurring_interval: SubscriptionRecurringInterval
    has_free_base: bool
    meter_prices: list[MeterPrice]
    monthly_fee: int | None
    recurring_interval_count: int = 1
    metadata: dict[str, str] | None = None


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

# Product definitions
PRODUCT_DEFINITIONS = [
    ProductDefinition(
        name="Basic",
        description="Basic features and pay-as-you-go pricing for compute, memory, and storage",
        recurring_interval=SubscriptionRecurringInterval.MONTH,
        has_free_base=True,
        monthly_fee=None,
        meter_prices=METER_PRICES,
        metadata={"tier": "basic"},
    ),
    ProductDefinition(
        name="Pro",
        description="Extended features and pay-as-you-go pricing for small teams and individuals",
        recurring_interval=SubscriptionRecurringInterval.MONTH,
        has_free_base=False,
        monthly_fee=5000,  # $50/month
        meter_prices=METER_PRICES,
        metadata={"tier": "pro"},
    ),
    ProductDefinition(
        name="Enterprise",
        description="Enterprise-grade features and pay-as-you-go pricing for large teams and organizations",
        recurring_interval=SubscriptionRecurringInterval.MONTH,
        has_free_base=False,
        monthly_fee=10000,  # $100/month
        meter_prices=METER_PRICES,
        metadata={"tier": "enterprise"},
    ),
]


async def setup_products(organization_id: str) -> dict:
    """Set up Polar products for LazyCloud billing"""
    logger.info(f"Setting up Polar products for organization: {organization_id}")

    polar = get_polar_service()

    if not polar.enabled:
        logger.error("Polar service is not enabled")
        raise RuntimeError("Polar service is not enabled")

    # Get all meters to create metered prices
    logger.info("Fetching meters from Polar...")
    meters = await polar.meters.list_meters(organization_id=organization_id)

    if not meters:
        logger.error("No meters found. Please run the meters setup script first.")
        raise RuntimeError("No meters found")

    # Create a mapping of meter name to meter ID
    meter_map = {meter.name: meter.id for meter in meters}
    logger.info(f"Found {len(meter_map)} meters: {list(meter_map.keys())}")

    created = 0
    updated = 0
    skipped = 0
    failed = 0

    # Process each product definition
    for product_def in PRODUCT_DEFINITIONS:
        name = product_def.name

        try:
            # Build the prices list
            prices: list[ProductCreateRecurringPrices] = []

            # Add free base subscription if specified
            if product_def.has_free_base:
                prices.append(ProductPriceFreeCreate())
            # Add fixed monthly fee if specified
            elif product_def.monthly_fee is not None:
                prices.append(
                    ProductPriceFixedCreate(
                        price_amount=product_def.monthly_fee,
                        price_currency="usd",
                    )
                )

            # Add metered prices for each meter
            for meter_price in product_def.meter_prices:
                meter_id = meter_map.get(meter_price.meter_name.value)
                if not meter_id:
                    logger.warning(
                        f"Meter '{meter_price.meter_name.value}' not found for product '{name}', skipping"
                    )
                    continue

                prices.append(
                    ProductPriceMeteredUnitCreate(
                        meter_id=meter_id,
                        unit_amount=meter_price.unit_amount,
                        price_currency="usd",
                    )
                )

            logger.info(f"Built {len(prices)} prices for product '{name}'")

            # Check if product already exists
            existing_product = await polar.products.get_product_by_name(
                name=name, organization_id=organization_id
            )

            if existing_product:
                logger.info(
                    f"Product '{name}' already exists with ID: {existing_product.id}"
                )

                # Update the product with new prices
                logger.info(f"Updating product '{name}' prices...")
                result = await polar.products.update_product(
                    product_id=existing_product.id,
                    prices=prices,
                )

                if result:
                    logger.info(f"Updated product '{name}' with {len(prices)} prices")
                    updated += 1
                else:
                    logger.error(f"Failed to update product '{name}'")
                    failed += 1
                continue

            # Create the product
            logger.info(f"Creating product '{name}' with {len(prices)} prices...")
            result = await polar.products.create_recurring_product(
                organization_id=organization_id,
                name=name,
                description=product_def.description,
                prices=prices,
                recurring_interval=product_def.recurring_interval,
                recurring_interval_count=product_def.recurring_interval_count,
                metadata=product_def.metadata,
            )

            if result:
                logger.info(f"Created product '{name}' with ID: {result.id}")
                created += 1
            else:
                logger.error(f"Failed to create product '{name}'")
                failed += 1

        except Exception as e:
            logger.error(f"Error processing product '{name}': {e}")
            failed += 1

    # Summary
    logger.info(
        f"Product setup complete: {created} created, {updated} updated, "
        f"{skipped} skipped, {failed} failed"
    )

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "total": len(PRODUCT_DEFINITIONS),
    }
