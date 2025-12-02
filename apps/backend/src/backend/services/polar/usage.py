from loguru import logger
from models.billing import (
    METER_METADATA_FIELDS,
    SECONDS_PER_HOUR,
    USAGE_EVENT_NAME,
    MeterNames,
)
from polar_sdk import Polar
from polar_sdk.models import EventCreateExternalCustomer, EventsIngest

from backend.database.usage import DailyUsageRecordPydantic


class PolarUsageModule:
    def __init__(self, client: Polar, enabled: bool):
        self.client = client
        self.enabled = enabled

    async def send_usage_event(
        self,
        external_customer_id: str,
        event_name: str,
        metadata: dict[str, str | float | int],
        idempotency_key: str | None = None,
    ) -> bool:
        """Send a usage event to Polar with optional idempotency key."""
        if not self.enabled:
            logger.debug(
                f"Polar disabled, skipping event: {event_name} for customer {external_customer_id}"
            )
            return False

        try:
            event = EventCreateExternalCustomer(
                name=event_name,
                external_customer_id=external_customer_id,
                metadata=metadata,
            )

            # Add idempotency key if provided
            if idempotency_key:
                event.idempotency_key = idempotency_key

            response = await self.client.events.ingest_async(
                request=EventsIngest(events=[event])
            )

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

    async def send_daily_usage(
        self,
        record: DailyUsageRecordPydantic,
        external_customer_id: str,
        idempotency_key: str,
    ) -> bool:
        """Send daily usage to Polar with idempotency key for exactly-once billing."""
        if not self.enabled:
            logger.debug(
                f"Polar disabled, skipping daily usage for {record.workspace_id}"
            )
            return False

        cpu_core_hours = record.cpu_core_seconds / SECONDS_PER_HOUR
        memory_gb_hours = record.memory_gb_seconds / SECONDS_PER_HOUR

        usage_metadata = {
            "workspace_id": str(record.workspace_id),
            "usage_record_id": str(record.id),
            "usage_date": str(record.usage_date),
            "intervals_collected": record.intervals_collected,
            "expected_intervals": record.expected_intervals,
            "raw_cpu_seconds": record.cpu_core_seconds,
            "raw_memory_seconds": record.memory_gb_seconds,
            METER_METADATA_FIELDS[MeterNames.CPU_USAGE]: cpu_core_hours,
            METER_METADATA_FIELDS[MeterNames.MEMORY_USAGE]: memory_gb_hours,
            METER_METADATA_FIELDS[
                MeterNames.STANDARD_STORAGE
            ]: record.standard_gb_hours,
            METER_METADATA_FIELDS[MeterNames.SHARED_STORAGE]: record.shared_gb_hours,
            METER_METADATA_FIELDS[MeterNames.BUILD_MINUTES]: record.build_minutes,
            METER_METADATA_FIELDS[
                MeterNames.PUBLIC_ENDPOINTS
            ]: record.public_endpoint_hours,
        }

        success = await self.send_usage_event(
            external_customer_id=external_customer_id,
            event_name=USAGE_EVENT_NAME,
            metadata=usage_metadata,
            idempotency_key=idempotency_key,
        )

        if success:
            logger.info(
                f"Sent daily usage for {record.workspace_id} ({record.usage_date}): "
                f"CPU={cpu_core_hours:.4f}h, Memory={memory_gb_hours:.4f}GB-h, "
                f"Standard={record.standard_gb_hours:.2f}GB-h, Shared={record.shared_gb_hours:.2f}GB-h, "
                f"Build={record.build_minutes:.2f}min, Endpoints={record.public_endpoint_hours:.2f}h"
            )

        return success
