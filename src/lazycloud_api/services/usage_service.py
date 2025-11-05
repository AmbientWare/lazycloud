import yaml
from loguru import logger

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic
from shared.models.billing import SECONDS_PER_HOUR, STORAGE_CLASS_EFS, STORAGE_CLASS_S3
from shared.responses.usage import ServiceUsageItem, UsageMetrics, VolumeUsageItem


def sanitize_volume_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")[:63].rstrip("-")


class UsageService:
    async def get_workspace_usage_breakdown(
        self,
        workspace_id: str,
        deployment_id: str | None = None,
    ) -> tuple[
        UsageMetrics | None,
        list[ServiceUsageItem] | None,
        list[VolumeUsageItem] | None,
        UsageRecordPydantic | None,
    ]:
        """Get usage breakdown for workspace, optionally filtered by deployment."""
        usage_record = await db.usage.get_latest_interval_usage(
            workspace_id=workspace_id
        )

        if not usage_record:
            return None, None, None, None

        service_usage = self._aggregate_service_usage(usage_record, deployment_id)
        volumes, s3_gb_hours, efs_gb_hours = self._aggregate_storage_usage(
            usage_record, deployment_id
        )

        metrics = UsageMetrics(
            cpu_core_hours=usage_record.cpu_core_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=usage_record.memory_gb_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=s3_gb_hours,
            efs_gb_hours=efs_gb_hours,
        )

        return metrics, list(service_usage.values()), volumes, usage_record

    def _aggregate_service_usage(
        self, usage_record: UsageRecordPydantic, deployment_id: str | None = None
    ) -> dict[str, ServiceUsageItem]:
        service_usage: dict[str, ServiceUsageItem] = {}

        for breakdown in usage_record.compute_breakdowns:
            # Filter by deployment if specified
            if deployment_id and (
                not breakdown.deployment_id or breakdown.deployment_id != deployment_id
            ):
                continue

            service_name = breakdown.service_name

            # Accumulate usage across multiple pods of the same service
            if service_name not in service_usage:
                service_usage[service_name] = ServiceUsageItem(
                    service_name=service_name,
                    cpu_core_seconds=breakdown.cpu_core_seconds,
                    memory_gb_seconds=breakdown.memory_gb_seconds,
                )
            else:
                service_usage[
                    service_name
                ].cpu_core_seconds += breakdown.cpu_core_seconds
                service_usage[
                    service_name
                ].memory_gb_seconds += breakdown.memory_gb_seconds

        return service_usage

    def _parse_deployment_volumes(
        self, compose_yaml: str, deployment_id: str
    ) -> set[str]:
        deployment_volumes: set[str] = set()

        try:
            compose_data = yaml.safe_load(compose_yaml)
            if compose_data and "volumes" in compose_data:
                for vol_name in compose_data["volumes"].keys():
                    sanitized = sanitize_volume_name(vol_name)
                    deployment_volumes.add(sanitized)
        except Exception as e:
            logger.warning(
                f"Failed to parse compose YAML for deployment {deployment_id}: {e}"
            )

        return deployment_volumes

    def _aggregate_storage_usage(
        self, usage_record: UsageRecordPydantic, deployment_id: str | None = None
    ) -> tuple[list[VolumeUsageItem], float, float]:
        s3_gb_hours = 0.0
        efs_gb_hours = 0.0
        volumes: list[VolumeUsageItem] = []

        for breakdown in usage_record.storage_breakdowns:
            # Filter by deployment if specified
            if deployment_id and (
                not breakdown.deployment_id or breakdown.deployment_id != deployment_id
            ):
                continue
            volumes.append(
                VolumeUsageItem(
                    volume_name=breakdown.pvc_name,
                    storage_class=breakdown.storage_class,
                    gb_hours=breakdown.gb_hours,
                )
            )

            # Aggregate totals by storage class
            if breakdown.storage_class == STORAGE_CLASS_S3:
                s3_gb_hours += breakdown.gb_hours
            elif breakdown.storage_class == STORAGE_CLASS_EFS:
                efs_gb_hours += breakdown.gb_hours

        return volumes, s3_gb_hours, efs_gb_hours
