from datetime import datetime

import yaml
from loguru import logger

from lazycloud_api.database import db
from lazycloud_api.database.usage import UsageRecordPydantic
from shared.models.billing import SECONDS_PER_HOUR, STORAGE_CLASS_EFS, STORAGE_CLASS_S3
from shared.responses.usage import ServiceUsageItem, UsageMetrics, VolumeUsageItem


def sanitize_volume_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")[:63].rstrip("-")


class UsageService:
    """Service for aggregating and processing usage data."""

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
            if deployment_id:
                if breakdown.deployment_id != deployment_id:
                    continue

            service_name = breakdown.service_name
            if not service_name or not service_name.strip():
                continue

            service_name = service_name.strip()

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
        """Parse volume names from Docker Compose YAML."""
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
            if deployment_id:
                if breakdown.deployment_id != deployment_id:
                    continue

            pvc_name = breakdown.pvc_name
            if not pvc_name or not pvc_name.strip():
                continue

            volumes.append(
                VolumeUsageItem(
                    volume_name=pvc_name.strip(),
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

    async def aggregate_workspace_usage_for_date_range(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        return_records: bool = False,
    ) -> UsageMetrics | tuple[UsageMetrics, list[UsageRecordPydantic]]:
        """Aggregate workspace-level usage totals across multiple records for a date range."""
        usage_records = await db.usage.get_workspace_usage(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )

        total_cpu_seconds = sum(r.cpu_core_seconds for r in usage_records)
        total_memory_seconds = sum(r.memory_gb_seconds for r in usage_records)
        total_s3_hours = sum(r.s3_gb_hours for r in usage_records)
        total_efs_hours = sum(r.efs_gb_hours for r in usage_records)

        metrics = UsageMetrics(
            cpu_core_hours=total_cpu_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=total_memory_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=total_s3_hours,
            efs_gb_hours=total_efs_hours,
        )

        if return_records:
            return metrics, usage_records
        return metrics

    def aggregate_deployment_usage_from_records(
        self,
        usage_records: list[UsageRecordPydantic],
        deployment_id: str,
    ) -> tuple[UsageMetrics, list[ServiceUsageItem], list[VolumeUsageItem]]:
        """Aggregate usage for a specific deployment across multiple usage records."""

        deployment_cpu_seconds = 0.0
        deployment_memory_seconds = 0.0
        deployment_s3_hours = 0.0
        deployment_efs_hours = 0.0
        aggregated_services: dict[str, ServiceUsageItem] = {}
        aggregated_volumes: dict[str, VolumeUsageItem] = {}

        for record in usage_records:
            for compute_breakdown in record.compute_breakdowns:
                if compute_breakdown.deployment_id == deployment_id:
                    deployment_cpu_seconds += compute_breakdown.cpu_core_seconds
                    deployment_memory_seconds += compute_breakdown.memory_gb_seconds

                    service_name = compute_breakdown.service_name
                    if service_name and service_name.strip():
                        service_name = service_name.strip()
                        if service_name not in aggregated_services:
                            aggregated_services[service_name] = ServiceUsageItem(
                                service_name=service_name,
                                cpu_core_seconds=0.0,
                                memory_gb_seconds=0.0,
                            )
                        aggregated_services[
                            service_name
                        ].cpu_core_seconds += compute_breakdown.cpu_core_seconds
                        aggregated_services[
                            service_name
                        ].memory_gb_seconds += compute_breakdown.memory_gb_seconds

            for storage_breakdown in record.storage_breakdowns:
                if storage_breakdown.deployment_id == deployment_id:
                    if storage_breakdown.storage_class == STORAGE_CLASS_S3:
                        deployment_s3_hours += storage_breakdown.gb_hours
                    elif storage_breakdown.storage_class == STORAGE_CLASS_EFS:
                        deployment_efs_hours += storage_breakdown.gb_hours

                    pvc_name = storage_breakdown.pvc_name
                    if pvc_name and pvc_name.strip():
                        volume_name = pvc_name.strip()
                        if volume_name not in aggregated_volumes:
                            aggregated_volumes[volume_name] = VolumeUsageItem(
                                volume_name=volume_name,
                                storage_class=storage_breakdown.storage_class,
                                gb_hours=0.0,
                            )
                        aggregated_volumes[
                            volume_name
                        ].gb_hours += storage_breakdown.gb_hours

        service_usage_list = list(aggregated_services.values())
        volume_usage_list = list(aggregated_volumes.values())

        metrics = UsageMetrics(
            cpu_core_hours=deployment_cpu_seconds / SECONDS_PER_HOUR,
            memory_gb_hours=deployment_memory_seconds / SECONDS_PER_HOUR,
            s3_gb_hours=deployment_s3_hours,
            efs_gb_hours=deployment_efs_hours,
        )

        return metrics, service_usage_list, volume_usage_list
