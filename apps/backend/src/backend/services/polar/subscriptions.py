import asyncio

from cachetools import TTLCache
from loguru import logger
from polar_sdk import Polar
from responses.billing import BillingCycleResponse


class PolarSubscriptionsModule:
    """Service for fetching subscription billing cycle information."""

    _cycle_cache: TTLCache = TTLCache(maxsize=100, ttl=360)
    _fetch_locks: dict[str, asyncio.Lock] = {}

    def __init__(self, client: Polar, enabled: bool):
        self.client = client
        self.enabled = enabled

    async def get_billing_cycle(
        self, external_customer_id: str, use_cache: bool = True
    ) -> BillingCycleResponse:
        """Get billing cycle dates for a customer's active subscription.

        Raises ValueError if no active subscription exists or Polar is disabled.
        """
        if not self.enabled:
            raise ValueError("Billing service is not available")

        # Check cache first (fast path, no lock needed)
        if use_cache and external_customer_id in self._cycle_cache:
            logger.debug(
                f"Using cached billing cycle for customer {external_customer_id}"
            )
            return self._cycle_cache[external_customer_id]

        # Get or create lock for this customer to prevent thundering herd
        if external_customer_id not in self._fetch_locks:
            self._fetch_locks[external_customer_id] = asyncio.Lock()
        lock = self._fetch_locks[external_customer_id]

        async with lock:
            # Double-check cache after acquiring lock
            if use_cache and external_customer_id in self._cycle_cache:
                logger.debug(
                    f"Using cached billing cycle for customer {external_customer_id}"
                )
                return self._cycle_cache[external_customer_id]

            # Fetch from Polar
            cycle = await self._fetch_billing_cycle(external_customer_id)
            self._cycle_cache[external_customer_id] = cycle
            logger.debug(f"Cached billing cycle for customer {external_customer_id}")
            return cycle

    async def _fetch_billing_cycle(
        self, external_customer_id: str
    ) -> BillingCycleResponse:
        """Fetch billing cycle from customer's active subscription."""
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

        if not subscription.current_period_start:
            raise ValueError(
                f"Subscription {subscription.id} has no current_period_start"
            )

        if not subscription.current_period_end:
            raise ValueError(
                f"Subscription {subscription.id} has no current_period_end"
            )

        logger.info(
            f"Fetched billing cycle for {external_customer_id}: "
            f"{subscription.current_period_start} - {subscription.current_period_end}"
        )

        return BillingCycleResponse(
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
        )

    def clear_cache(self, external_customer_id: str | None = None) -> None:
        """Clear billing cycle cache for a specific customer or all customers."""
        if external_customer_id:
            if external_customer_id in self._cycle_cache:
                del self._cycle_cache[external_customer_id]
                logger.info(
                    f"Cleared billing cycle cache for customer {external_customer_id}"
                )
        else:
            self._cycle_cache.clear()
            logger.info("Cleared all billing cycle cache")
