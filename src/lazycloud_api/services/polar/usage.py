from loguru import logger
from polar_sdk import Polar
from polar_sdk.models import EventCreateExternalCustomer, EventsIngest

from lazycloud_api.database.usage import UsageRecordPydantic
from shared.models.billing import (
    METER_METADATA_FIELDS,
    SECONDS_PER_HOUR,
    USAGE_EVENT_NAME,
    MeterNames,
)


class PolarUsageModule:
    """Service for managing billing operations with Polar."""

    def __init__(self, client: Polar, enabled: bool):
        """Initialize the Polar client."""
        self.client = client
        self.enabled = enabled

    async def send_usage_event(
        self,
        external_customer_id: str,
        event_name: str,
        quantity: float,
        metadata: dict[str, str | float | int],
    ) -> bool:
        """Send a usage event to Polar"""
        if not self.enabled:
            logger.debug(
                f"Polar disabled, skipping event: {event_name} for customer {external_customer_id}"
            )
            return False

        try:
            # Send usage event to Polar and capture response
            response = await self.client.events.ingest_async(
                request=EventsIngest(
                    events=[
                        EventCreateExternalCustomer(
                            name=event_name,
                            external_customer_id=external_customer_id,
                            metadata=metadata,
                        )
                    ]
                )
            )

            # Validate response - check that events were inserted
            if response.inserted < 1:
                logger.error(
                    f"Polar API returned inserted={response.inserted} (expected >= 1) "
                    f"for event {event_name} customer {external_customer_id}"
                )
                return False

            return True

        except Exception as e:
            logger.exception(
                f"Failed to send Polar usage event {event_name} for customer {external_customer_id}: {e}"
            )
            return False

    async def send_workspace_usage(
        self, usage_record: UsageRecordPydantic, external_customer_id: str
    ) -> bool:
        """Send workspace usage data to Polar as a single event with all metrics"""
        if not self.enabled:
            logger.debug(
                f"Polar disabled, skipping workspace usage for {usage_record.workspace_id}"
            )
            return False

        # Convert to hours for billing (keep raw values for audit trail)
        cpu_core_seconds = usage_record.cpu_core_seconds
        memory_gb_seconds = usage_record.memory_gb_seconds
        s3_gb_hours = usage_record.s3_gb_hours
        efs_gb_hours = usage_record.efs_gb_hours
        build_minutes = usage_record.build_minutes
        public_endpoint_hours = usage_record.public_endpoint_hours

        cpu_core_hours = cpu_core_seconds / SECONDS_PER_HOUR
        memory_gb_hours = memory_gb_seconds / SECONDS_PER_HOUR

        # Single event with all metrics in metadata
        usage_metadata = {
            "workspace_id": str(usage_record.workspace_id),
            "usage_record_id": str(usage_record.id),
            "collection_start": usage_record.collection_start.isoformat(),
            "collection_end": usage_record.collection_end.isoformat(),
            "record_type": usage_record.record_type,
            # Raw values for audit trail
            # NOTE: the volume data is already in raw form
            "raw_cpu_seconds": cpu_core_seconds,
            "raw_memory_seconds": memory_gb_seconds,
            # Meter-specific fields (these are what meters aggregate on)
            METER_METADATA_FIELDS[MeterNames.CPU_USAGE]: cpu_core_hours,
            METER_METADATA_FIELDS[MeterNames.MEMORY_USAGE]: memory_gb_hours,
            METER_METADATA_FIELDS[MeterNames.STANDARD_STORAGE]: s3_gb_hours,
            METER_METADATA_FIELDS[MeterNames.PREMIUM_STORAGE]: efs_gb_hours,
            METER_METADATA_FIELDS[MeterNames.BUILD_MINUTES]: build_minutes,
            METER_METADATA_FIELDS[MeterNames.PUBLIC_ENDPOINTS]: public_endpoint_hours,
        }

        # Calculate total quantity
        total_quantity = (
            cpu_core_hours
            + memory_gb_hours
            + s3_gb_hours
            + efs_gb_hours
            + build_minutes
            + public_endpoint_hours
        )

        # Send single usage event with all metrics
        success = await self.send_usage_event(
            external_customer_id=external_customer_id,
            event_name=USAGE_EVENT_NAME,
            quantity=total_quantity,
            metadata=usage_metadata,
        )

        if success:
            logger.info(
                f"Successfully sent usage event for workspace {usage_record.workspace_id}: "
                f"CPU={cpu_core_hours:.4f}h, Memory={memory_gb_hours:.4f}GB-h, "
                f"S3={s3_gb_hours:.2f}GB-h, EFS={efs_gb_hours:.2f}GB-h, "
                f"Build={build_minutes:.2f}min, Endpoints={public_endpoint_hours:.2f}h "
                f"(total={total_quantity:.4f})"
            )

        return success
