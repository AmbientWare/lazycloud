import asyncio

from cachetools import TTLCache
from loguru import logger
from models.billing import MeterNames, UsageUnits
from polar_sdk import Polar
from pydantic import BaseModel

from .products import PolarProductsModule


class MeterPrices(BaseModel):
    """Meter prices returned from Polar subscriptions (per unit in USD)."""

    cpu_price_per_unit: float
    memory_price_per_unit: float
    standard_price_per_unit: float
    shared_price_per_unit: float
    build_minutes_price_per_unit: float
    endpoint_hours_price_per_unit: float


class PolarPricingModule:
    """Service for fetching and caching meter prices from Polar."""

    _price_cache: TTLCache = TTLCache(maxsize=100, ttl=360)
    _meter_cache: TTLCache = TTLCache(maxsize=50, ttl=360)
    _fetch_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self, client: Polar, enabled: bool, products_module: PolarProductsModule
    ):
        """Initialize the Polar pricing module."""
        self.client = client
        self.enabled = enabled
        self.products_module = products_module

    async def get_meter_prices(
        self, external_customer_id: str, use_cache: bool = True
    ) -> MeterPrices:
        """Get current meter prices for a customer."""
        if not self.enabled:
            raise ValueError("Polar is disabled. Cannot fetch meter prices.")

        # Check cache first (fast path, no lock needed)
        if use_cache and external_customer_id in self._price_cache:
            logger.debug(f"Using cached prices for customer {external_customer_id}")
            return self._price_cache[external_customer_id]

        # Get or create lock for this customer to prevent thundering herd
        if external_customer_id not in self._fetch_locks:
            self._fetch_locks[external_customer_id] = asyncio.Lock()
        lock = self._fetch_locks[external_customer_id]

        async with lock:
            # Double-check cache after acquiring lock (another request may have populated it)
            if use_cache and external_customer_id in self._price_cache:
                logger.debug(f"Using cached prices for customer {external_customer_id}")
                return self._price_cache[external_customer_id]

            # Fetch fresh prices
            try:
                prices = await self._fetch_prices_from_subscription(
                    external_customer_id
                )
                self._price_cache[external_customer_id] = prices
                logger.debug(f"Cached prices for customer {external_customer_id}")
                return prices

            except Exception as e:
                logger.error(
                    f"Failed to fetch meter prices for customer {external_customer_id}: {e}"
                )
                raise

    async def _fetch_prices_from_subscription(
        self, external_customer_id: str
    ) -> MeterPrices:
        """Fetch prices from customer's active subscription"""
        # Get active subscription for customer
        subscriptions_response = await self.client.subscriptions.list_async(
            external_customer_id=external_customer_id,
            active=True,
            limit=1,
        )

        if (
            not subscriptions_response
            or not subscriptions_response.result
            or not subscriptions_response.result.items
        ):
            raise ValueError(
                f"No active subscription found for customer {external_customer_id}"
            )

        subscription = subscriptions_response.result.items[0]
        if not subscription.product:
            raise ValueError(
                f"Subscription {subscription.id} has no associated product"
            )

        product = await self.products_module.get_product(subscription.product.id)
        if not product:
            raise ValueError(f"Product {subscription.product.id} not found")

        if not product.prices:
            raise ValueError(
                f"Product {subscription.product.id} has no prices configured"
            )

        # Extract metered prices and map to meter names
        meter_prices_dict: dict[str, float] = {}
        for price in product.prices:
            # Check if this is a metered price (has meter_id)
            meter_id = getattr(price, "meter_id", None)
            if meter_id:
                # Get meter (with caching)
                if meter_id in self._meter_cache:
                    meter = self._meter_cache[meter_id]
                else:
                    meter = await self.client.meters.get_async(id=meter_id)
                    if meter:
                        self._meter_cache[meter_id] = meter

                if not meter:
                    logger.warning(f"Meter {meter_id} not found, skipping")
                    continue

                unit_amount = getattr(price, "unit_amount", None)
                if unit_amount is None:
                    logger.warning(
                        f"Price for meter {meter.name} has no unit_amount, skipping"
                    )
                    continue

                # Convert from cents to dollars
                price_per_unit = UsageUnits.cents_to_dollars(float(unit_amount))
                meter_prices_dict[meter.name] = price_per_unit

        # Verify all required meters are present
        missing_meters = [
            meter_name.value
            for meter_name in MeterNames
            if meter_name.value not in meter_prices_dict
        ]
        if missing_meters:
            raise ValueError(
                f"Product {subscription.product.id} is missing required meter prices: {missing_meters}"
            )

        logger.info(
            f"Fetched meter prices for {external_customer_id}: "
            f"CPU=${meter_prices_dict[MeterNames.CPU_USAGE.value]:.4f}, "
            f"Memory=${meter_prices_dict[MeterNames.MEMORY_USAGE.value]:.4f}, "
            f"Standard=${meter_prices_dict[MeterNames.STANDARD_STORAGE.value]:.4f}, "
            f"Shared=${meter_prices_dict[MeterNames.SHARED_STORAGE.value]:.4f}, "
            f"Build=${meter_prices_dict[MeterNames.BUILD_MINUTES.value]:.4f}, "
            f"Endpoints=${meter_prices_dict[MeterNames.PUBLIC_ENDPOINTS.value]:.6f}"
        )

        return MeterPrices(
            cpu_price_per_unit=meter_prices_dict[MeterNames.CPU_USAGE.value],
            memory_price_per_unit=meter_prices_dict[MeterNames.MEMORY_USAGE.value],
            standard_price_per_unit=meter_prices_dict[
                MeterNames.STANDARD_STORAGE.value
            ],
            shared_price_per_unit=meter_prices_dict[MeterNames.SHARED_STORAGE.value],
            build_minutes_price_per_unit=meter_prices_dict[
                MeterNames.BUILD_MINUTES.value
            ],
            endpoint_hours_price_per_unit=meter_prices_dict[
                MeterNames.PUBLIC_ENDPOINTS.value
            ],
        )

    def clear_cache(self, external_customer_id: str | None = None) -> None:
        """Clear price cache for a specific customer or all customers"""
        if external_customer_id:
            if external_customer_id in self._price_cache:
                del self._price_cache[external_customer_id]
                logger.info(f"Cleared price cache for customer {external_customer_id}")
        else:
            self._price_cache.clear()
            logger.info("Cleared all price cache")
