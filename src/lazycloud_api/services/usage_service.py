import uuid

import yaml
from loguru import logger

from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.usage import UsageRecordPydantic
from shared.models.billing import STORAGE_CLASS_EFS, STORAGE_CLASS_S3
from shared.responses.usage import ServiceUsageItem, UsageMetrics, VolumeUsageItem


def sanitize_volume_name(name: str) -> str:
    """Sanitize volume name to match Kubernetes PVC naming (DNS-1123)"""
    return name.lower().replace("_", "-").replace(".", "-")[:63].rstrip("-")


class UsageService:
    """Service for processing and aggregating usage data"""

    async def get_workspace_usage_breakdown(
        self,
        workspace_id: uuid.UUID,
        deployment: ComposeDeploymentPydantic,
    ) -> tuple[
        UsageMetrics | None,
        list[ServiceUsageItem] | None,
        list[VolumeUsageItem] | None,
        UsageRecordPydantic | None,
    ]:
        """Get detailed usage breakdown for a specific deployment"""
        # Get latest interval usage record (hourly, 15-min, etc.)
        usage_record = await db.usage.get_latest_interval_usage(
            workspace_id=workspace_id
        )

        if not usage_record:
            return None, None, None, None

        # Parse pod names to extract service names and aggregate compute
        service_usage = self._aggregate_service_usage(usage_record)

        # Get deployment volumes and filter storage
        deployment_volumes = self._parse_deployment_volumes(
            deployment.compose_yaml, deployment.id
        )
        volumes, s3_gb_hours, efs_gb_hours = self._aggregate_storage_usage(
            usage_record, deployment_volumes
        )

        # Build metrics
        metrics = UsageMetrics(
            cpu_core_hours=usage_record.cpu_core_seconds / 3600,
            memory_gb_hours=usage_record.memory_gb_seconds / 3600,
            s3_gb_hours=s3_gb_hours,
            efs_gb_hours=efs_gb_hours,
        )

        return metrics, list(service_usage.values()), volumes, usage_record

    def _aggregate_service_usage(
        self, usage_record: UsageRecordPydantic
    ) -> dict[str, ServiceUsageItem]:
        """Parse compute breakdowns and aggregate by service name"""
        service_usage: dict[str, ServiceUsageItem] = {}

        for breakdown in usage_record.compute_breakdowns:
            # Extract service name from pod name
            # Pod naming pattern: {service-name}-{replicaset-hash}-{pod-hash}
            pod_parts = breakdown.pod_name.split("-")
            if len(pod_parts) >= 3:
                service_name = "-".join(pod_parts[:-2])
            else:
                service_name = pod_parts[0]

            if service_name not in service_usage:
                service_usage[service_name] = ServiceUsageItem(
                    service_name=service_name,
                    cpu_core_seconds=breakdown.cpu_core_seconds,
                    memory_gb_seconds=breakdown.memory_gb_seconds,
                )
            else:
                # Accumulate usage for multiple pods of the same service
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
        """Parse compose YAML to extract sanitized volume names"""
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
        self, usage_record: UsageRecordPydantic, deployment_volumes: set[str]
    ) -> tuple[list[VolumeUsageItem], float, float]:
        """Filter and aggregate storage by class (S3 vs EFS), returning individual volumes and totals"""
        s3_gb_hours = 0.0
        efs_gb_hours = 0.0
        volumes: list[VolumeUsageItem] = []

        for breakdown in usage_record.storage_breakdowns:
            # Check if this PVC belongs to the deployment
            if breakdown.pvc_name in deployment_volumes:
                # Add individual volume
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
