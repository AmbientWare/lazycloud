from enum import StrEnum

from loguru import logger
from polar_sdk import Polar

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic


class PolarServer(StrEnum):
    """Polar server environment."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


class PolarService:
    """Service for managing billing operations with Polar."""

    def __init__(self, access_token: str, is_sandbox: bool):
        """Initialize the Polar client."""
        self.enabled = bool(access_token)
        self.is_sandbox = is_sandbox
        self.client = None

        if self.enabled:
            try:
                self.client = Polar(
                    access_token=access_token,
                    server=PolarServer.SANDBOX.value
                    if self.is_sandbox
                    else PolarServer.PRODUCTION.value,
                )
                logger.info("Polar billing service initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Polar client: {e}")
                self.enabled = False
        else:
            logger.warning(
                "Polar billing service disabled: POLAR_ACCESS_TOKEN not configured"
            )

    async def send_usage_event(
        self,
        external_customer_id: str,
        event_name: str,
        quantity: float,
        metadata: dict[str, str | float | int],
    ) -> bool:
        """
        Send a usage event to Polar"""
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
            self.client.usage.ingest(
                event=event_name,
                external_customer_id=external_customer_id,
                metadata=metadata,
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
            return {"cpu": False, "memory": False, "storage": False}

        # Convert to hours for billing (keep raw values for audit trail)
        cpu_core_seconds = usage_record.cpu_core_seconds
        memory_gb_seconds = usage_record.memory_gb_seconds
        storage_gb_hours = usage_record.storage_gb_hours

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
            event_name="cpu_usage",
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
            event_name="memory_usage",
            quantity=memory_gb_hours,
            metadata=memory_metadata,
        )

        # Send Storage usage event (in hours)
        storage_metadata = {
            **base_metadata,
            "quantity": storage_gb_hours,
            "unit": "gb_hours",
        }
        results["storage"] = await self.send_usage_event(
            external_customer_id=polar_customer_id,
            event_name="storage_usage",
            quantity=storage_gb_hours,
            metadata=storage_metadata,
        )

        # Log summary
        success_count = sum(results.values())
        if success_count == 3:
            logger.info(
                f"Successfully sent all usage events for workspace {usage_record.workspace_id}: "
                f"CPU={cpu_core_hours:.4f}h, Memory={memory_gb_hours:.4f}GB-h, Storage={storage_gb_hours:.2f}GB-h"
            )
        else:
            logger.warning(
                f"Partial success sending usage for workspace {usage_record.workspace_id}: "
                f"{success_count}/3 events sent"
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
