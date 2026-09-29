from __future__ import annotations

from dataclasses import dataclass

from agent.binary import AgentBinarySettings
from compute.reclaim import ComputeReclaimPolicy
from observability.settings import (
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from provider_clients.settings import (
    AwsAccountConnectionSettings,
    AwsCapacitySettings,
)
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage_client.s3 import S3ObjectStoreSettings


@dataclass(frozen=True, slots=True)
class SchedulerObservabilitySettings:
    workspace_changes: WorkspaceChangeStreamSettings


@dataclass(frozen=True, slots=True)
class SchedulerStorageSettings:
    object_store: S3ObjectStoreSettings
    image_archive: ImageArchiveSettings
    retention: RetentionSettings
    volume_metering: VolumeMeteringSettings
    workload_image_registry_repository: str = ""


@dataclass(frozen=True, slots=True)
class SchedulerCapacitySettings:
    aws_connections: AwsAccountConnectionSettings
    aws_capacity: AwsCapacitySettings
    agent_binaries: AgentBinarySettings
    reclaim: ComputeReclaimPolicy
