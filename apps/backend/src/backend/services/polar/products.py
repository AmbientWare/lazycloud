import asyncio

from cachetools import TTLCache
from loguru import logger
from polar_sdk import Polar
from polar_sdk.models import (
    Product,
    ProductCreateRecurring,
    ProductCreateRecurringMetadata,
    ProductCreateRecurringPrices,
    ProductUpdate,
    SubscriptionRecurringInterval,
)


class PolarProductsModule:
    """Service for managing product operations with Polar."""

    _product_cache: TTLCache = TTLCache(maxsize=50, ttl=360)
    _fetch_locks: dict[str, asyncio.Lock] = {}

    def __init__(self, client: Polar, enabled: bool):
        """Initialize the Polar products module."""
        self.client = client
        self.enabled = enabled

    async def create_recurring_product(
        self,
        organization_id: str,
        name: str,
        prices: list[ProductCreateRecurringPrices],
        recurring_interval: SubscriptionRecurringInterval,
        metadata: dict[str, ProductCreateRecurringMetadata] | None = None,
        description: str | None = None,
        recurring_interval_count: int = 1,
    ) -> Product | None:
        """Create a new product in Polar"""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping product creation for {name}")
            return None

        try:
            # Create the product in Polar
            result = await self.client.products.create_async(
                request=ProductCreateRecurring(
                    name=name,
                    prices=prices,
                    recurring_interval=recurring_interval,
                    recurring_interval_count=recurring_interval_count,
                    metadata=metadata,
                    description=description,
                )
            )

            logger.info(f"Created Polar product: {result.name}")
            return result

        except Exception as e:
            logger.error(
                f"Failed to create Polar product {name} for org {organization_id}: {e}"
            )
            return None

    async def get_product(self, product_id: str) -> Product | None:
        """Get a product from Polar by ID (cached for 1 hour)."""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping product retrieval for {product_id}")
            return None

        # Fast path: check cache without lock
        if product_id in self._product_cache:
            logger.debug(f"Using cached Polar product: {product_id}")
            return self._product_cache[product_id]

        # Get or create lock for this product to prevent thundering herd
        if product_id not in self._fetch_locks:
            self._fetch_locks[product_id] = asyncio.Lock()
        lock = self._fetch_locks[product_id]

        async with lock:
            # Double-check cache after acquiring lock
            if product_id in self._product_cache:
                logger.debug(f"Using cached Polar product: {product_id}")
                return self._product_cache[product_id]

            try:
                result = await self.client.products.get_async(id=product_id)
                self._product_cache[product_id] = result
                logger.info(f"Retrieved and cached Polar product: {product_id}")
                return result

            except Exception as e:
                logger.error(f"Failed to get Polar product {product_id}: {e}")
                return None

    async def list_products(
        self, organization_id: str, is_archived: bool | None = None
    ) -> list[Product]:
        """List products from Polar for the organization"""
        if not self.enabled:
            logger.info("Polar disabled, skipping product listing")
            return []

        try:
            # List products for the organization
            result = await self.client.products.list_async(
                organization_id=organization_id,
                is_archived=is_archived,
            )
            if result is None or result.result is None:
                return []

            logger.info(f"Retrieved {len(result.result.items)} Polar products")
            return result.result.items

        except Exception as e:
            logger.error(
                f"Failed to list Polar products for org {organization_id}: {e}"
            )
            return []

    async def update_product(
        self,
        product_id: str,
        name: str | None = None,
        description: str | None = None,
        prices: list[ProductCreateRecurringPrices] | None = None,
        recurring_interval: SubscriptionRecurringInterval | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Product | None:
        """Update a product in Polar"""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping product update for {product_id}")
            return None

        try:
            # Build update kwargs with only provided fields
            update_kwargs = {}
            if name is not None:
                update_kwargs["name"] = name
            if description is not None:
                update_kwargs["description"] = description
            if prices is not None:
                update_kwargs["prices"] = prices
            if recurring_interval is not None:
                update_kwargs["recurring_interval"] = recurring_interval
            if metadata is not None:
                update_kwargs["metadata"] = metadata

            product_update = ProductUpdate(**update_kwargs)

            # Update the product
            result = await self.client.products.update_async(
                id=product_id, product_update=product_update
            )

            logger.info(f"Updated Polar product: {product_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to update Polar product {product_id}: {e}")
            return None

    async def archive_product(self, product_id: str) -> Product | None:
        """Archive a product in Polar (soft delete)."""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping product archive for {product_id}")
            return None

        try:
            product_update = ProductUpdate(is_archived=True)
            result = await self.client.products.update_async(
                id=product_id, product_update=product_update
            )
            logger.info(f"Archived Polar product: {product_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to archive Polar product {product_id}: {e}")
            return None

    async def get_product_by_name(
        self, name: str, organization_id: str
    ) -> Product | None:
        """Get a product from Polar by name and organization"""
        if not self.enabled:
            logger.info(
                f"Polar disabled, skipping product lookup for name {name} in org "
                f"{organization_id}"
            )
            return None

        try:
            # List only active products for the organization and find by name
            products = await self.list_products(
                organization_id=organization_id, is_archived=False
            )

            for product in products:
                if product.name == name:
                    return product

            logger.info(f"No Polar product found with name: {name}")
            return None

        except Exception as e:
            logger.error(f"Failed to get Polar product by name {name}: {e}")
            return None
