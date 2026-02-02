from loguru import logger
from polar_sdk import Polar
from polar_sdk.models import Customer, CustomerCreate, CustomerCreateMetadata


class PolarCustomersModule:
    """Service for managing customer operations with Polar."""

    def __init__(self, client: Polar, enabled: bool):
        """Initialize the Polar customers module."""
        self.client = client
        self.enabled = enabled

    async def create_customer(
        self,
        email: str,
        external_id: str,
        name: str | None = None,
        metadata: dict[str, CustomerCreateMetadata] | None = None,
    ) -> Customer | None:
        """Create a new customer in Polar"""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping customer creation for {external_id}")
            return None

        try:
            # Create the customer in Polar
            result = await self.client.customers.create_async(
                request=CustomerCreate(
                    email=email,
                    name=name,
                    external_id=external_id,
                    metadata=metadata,
                )
            )

            logger.info(f"Created Polar customer: {result.external_id}")
            return result

        except Exception as e:
            logger.error(
                f"Failed to create Polar customer for {external_id} ({email}): {e}"
            )
            return None

    async def delete_customer(self, external_id: str) -> bool:
        """Delete a customer from Polar"""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping customer deletion for {external_id}")
            return False

        try:
            # Delete the customer from Polar
            await self.client.customers.delete_external_async(external_id=external_id)

            logger.info(f"Deleted Polar customer: {external_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete Polar customer {external_id}: {e}")
            return False

    async def get_customer(self, external_id: str) -> Customer | None:
        """Get a customer from Polar by external ID"""
        if not self.enabled:
            logger.info(
                f"Polar disabled, skipping customer retrieval for {external_id}"
            )
            return None

        try:
            result = await self.client.customers.get_external_async(
                external_id=external_id
            )
            logger.info(f"Retrieved Polar customer: {external_id}")
            return result

        except Exception as e:
            logger.warning(f"Failed to get Polar customer {external_id}: {e}")
            return None
