from loguru import logger
from polar_sdk import Polar
from polar_sdk.models import (
    Filter,
    Meter,
    MeterCreate,
    MeterCreateAggregation,
    MeterUpdate,
)


class PolarMetersModule:
    """Service for managing meter operations with Polar."""

    def __init__(self, client: Polar, enabled: bool):
        """Initialize the Polar meters module."""
        self.client = client
        self.enabled = enabled

    async def create_meter(
        self,
        organization_id: str,
        name: str,
        filter_: Filter,
        aggregation: MeterCreateAggregation,
        metadata: dict[str, str] | None = None,
    ) -> Meter | None:
        """Create a new meter in Polar"""
        if not self.enabled:
            logger.info(
                f"Polar disabled, skipping meter creation for {name} in org "
                f"{organization_id}"
            )
            return None

        try:
            # Create the meter in Polar using the SDK
            result = await self.client.meters.create_async(
                request=MeterCreate(
                    name=name,
                    filter_=filter_,
                    aggregation=aggregation,
                    metadata=metadata,
                )
            )

            logger.info(f"Created Polar meter: {result.name}")
            return result

        except Exception as e:
            logger.error(
                f"Failed to create Polar meter {name} for org {organization_id}: {e}"
            )
            return None

    async def get_meter(self, meter_id: str) -> Meter | None:
        """Get a meter from Polar by ID"""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping meter retrieval for {meter_id}")
            return None

        try:
            result = await self.client.meters.get_async(id=meter_id)
            logger.info(f"Retrieved Polar meter: {meter_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to get Polar meter {meter_id}: {e}")
            return None

    async def list_meters(
        self,
        organization_id: str | None = None,
        query: str | None = None,
        is_archived: bool | None = None,
    ) -> list[Meter]:
        """List meters from Polar, optionally filtered by organization"""
        if not self.enabled:
            logger.info("Polar disabled, skipping meter listing")
            return []

        try:
            # Build kwargs for list call
            kwargs = {}
            if organization_id:
                kwargs["organization_id"] = organization_id
            if query:
                kwargs["query"] = query
            if is_archived is not None:
                kwargs["is_archived"] = is_archived

            # List meters with filters
            meter_list_response = await self.client.meters.list_async(**kwargs)

            meters = meter_list_response.result.items
            logger.info(f"Retrieved {len(meters)} Polar meters")
            return meters

        except Exception as e:
            logger.error(f"Failed to list Polar meters: {e}")
            return []

    async def update_meter(
        self,
        meter_id: str,
        name: str | None = None,
        filter_: Filter | None = None,
        aggregation: MeterCreateAggregation | None = None,
    ) -> Meter | None:
        """Update a meter in Polar"""
        if not self.enabled:
            logger.info(f"Polar disabled, skipping meter update for {meter_id}")
            return None

        try:
            # Build meter_update with only provided fields
            update_kwargs = {}
            if name is not None:
                update_kwargs["name"] = name
            if filter_ is not None:
                update_kwargs["filter_"] = filter_
            if aggregation is not None:
                update_kwargs["aggregation"] = aggregation

            # Update the meter
            result = await self.client.meters.update_async(
                id=meter_id, meter_update=MeterUpdate(**update_kwargs)
            )

            logger.info(f"Updated Polar meter: {meter_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to update Polar meter {meter_id}: {e}")
            return None

    async def get_meter_by_name(self, name: str, organization_id: str) -> Meter | None:
        """Get a meter from Polar by name and organization"""
        if not self.enabled:
            logger.info(
                f"Polar disabled, skipping meter lookup for name {name} in org "
                f"{organization_id}"
            )
            return None

        try:
            # List meters filtered by name query
            meters = await self.list_meters(organization_id=organization_id, query=name)

            # Find exact name match
            for meter in meters:
                if meter.name == name:
                    logger.info(f"Found Polar meter by name: {name} -> {meter.name}")
                    return meter

        except Exception as e:
            logger.error(f"Failed to get Polar meter by name {name}: {e}")
            return None

        logger.info(f"No Polar meter found with name: {name}")
        return None
