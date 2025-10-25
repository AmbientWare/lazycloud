from loguru import logger
from polar_sdk import Polar
from polar_sdk.models import EventCreateExternalCustomer, EventsIngest

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic
from shared.models.billing import METERS_EVENT_MAP, MeterNames


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

        # Skip zero usage events (Polar may reject them)
        if quantity == 0:
            logger.debug(
                f"Skipping zero usage event: {event_name} for customer {external_customer_id}"
            )
            return True  # Consider it success

        try:
            # Send usage event to Polar
            self.client.events.ingest(
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

            logger.info(
                f"Sent Polar usage event: {event_name} = {quantity} for customer {external_customer_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to send Polar usage event {event_name} for customer {external_customer_id}: {e}"
            )
            return False

    async def send_workspace_usage(
        self, usage_record: UsageRecordPydantic, polar_customer_id: str
    ) -> dict[str, bool]:
        """
        Send workspace usage data to Polar as separate events for CPU, memory, and storage"""
        if not self.enabled:
            logger.debug(
                f"Polar disabled, skipping workspace usage for {usage_record.workspace_id}"
            )
            return {
                "cpu": False,
                "memory": False,
                "s3_storage": False,
                "efs_storage": False,
            }

        # Convert to hours for billing (keep raw values for audit trail)
        cpu_core_seconds = usage_record.cpu_core_seconds
        memory_gb_seconds = usage_record.memory_gb_seconds
        s3_gb_hours = usage_record.s3_gb_hours
        efs_gb_hours = usage_record.efs_gb_hours

        cpu_core_hours = cpu_core_seconds / 3600
        memory_gb_hours = memory_gb_seconds / 3600

        # Common metadata for all events - this is used for deduplication
        base_metadata = {
            "workspace_id": str(usage_record.workspace_id),
            "usage_record_id": str(usage_record.id),
            "collection_start": usage_record.collection_start.isoformat(),
            "collection_end": usage_record.collection_end.isoformat(),
            "record_type": usage_record.record_type,
            "raw_cpu_seconds": cpu_core_seconds,
            "raw_memory_seconds": memory_gb_seconds,
        }

        results = {}

        # Send CPU usage event (in hours)
        cpu_metadata = {
            **base_metadata,
            "quantity": cpu_core_hours,
            "unit": "core_hours",
        }
        results["cpu"] = await self.send_usage_event(
            external_customer_id=polar_customer_id,
            event_name=METERS_EVENT_MAP[MeterNames.CPU_USAGE],
            quantity=cpu_core_hours,
            metadata=cpu_metadata,
        )

        # Send Memory usage event (in hours)
        memory_metadata = {
            **base_metadata,
            "quantity": memory_gb_hours,
            "unit": "gb_hours",
        }
        results["memory"] = await self.send_usage_event(
            external_customer_id=polar_customer_id,
            event_name=METERS_EVENT_MAP[MeterNames.MEMORY_USAGE],
            quantity=memory_gb_hours,
            metadata=memory_metadata,
        )

        # Send S3 Storage usage event
        s3_storage_metadata = {
            **base_metadata,
            "quantity": s3_gb_hours,
            "unit": "gb_hours",
            "storage_class": "s3-sc",
        }
        results["s3_storage"] = await self.send_usage_event(
            external_customer_id=polar_customer_id,
            event_name=METERS_EVENT_MAP[MeterNames.NORMAL_STORAGE],
            quantity=s3_gb_hours,
            metadata=s3_storage_metadata,
        )

        # Send EFS Storage usage event
        efs_storage_metadata = {
            **base_metadata,
            "quantity": efs_gb_hours,
            "unit": "gb_hours",
            "storage_class": "efs-sc",
        }
        results["efs_storage"] = await self.send_usage_event(
            external_customer_id=polar_customer_id,
            event_name=METERS_EVENT_MAP[MeterNames.HIGH_PERFORMANCE_STORAGE],
            quantity=efs_gb_hours,
            metadata=efs_storage_metadata,
        )

        # Log summary
        success_count = sum(results.values())
        if success_count == 4:
            logger.info(
                f"Successfully sent all usage events for workspace {usage_record.workspace_id}: "
                f"CPU={cpu_core_hours:.4f}h, Memory={memory_gb_hours:.4f}GB-h, "
                f"S3={s3_gb_hours:.2f}GB-h, EFS={efs_gb_hours:.2f}GB-h"
            )
        else:
            logger.warning(
                f"Failed to send some usage events for workspace {usage_record.workspace_id} "
                f"(sent {success_count}/4): {results}"
            )

        return results

    async def get_workspace_owner_polar_id(self, workspace_id: str) -> str | None:
        """Get the Polar customer ID for the workspace owner"""
        try:
            user = await db.workspaces.aget_owner_user(workspace_id)

            if not user:
                logger.warning(f"No owner found for workspace {workspace_id}")
                return None

            if not user.polar_id:
                logger.warning(
                    f"User {user.id} (workspace owner for {workspace_id}) has no polar_id set"
                )
                return None

            return user.polar_id

        except Exception as e:
            logger.error(
                f"Error getting workspace owner polar_id for {workspace_id}: {e}"
            )
            return None
