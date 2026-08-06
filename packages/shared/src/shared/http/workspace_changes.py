from __future__ import annotations

from datetime import datetime

from pydantic import field_validator

from shared.enums import StringEnum
from shared.http.base import HttpModel


class WorkspaceChangeTopic(StringEnum):
    Apps = "apps"
    Deployments = "deployments"
    Workloads = "workloads"
    Tasks = "tasks"
    Containers = "containers"
    ComputeUnits = "compute.units"
    ComputeMachines = "compute.machines"
    ComputeWorkers = "compute.workers"
    ComputeAgents = "compute.agents"
    ComputeProviders = "compute.providers"
    ComputeConnections = "compute.connections"
    StorageSecrets = "storage.secrets"
    StorageVolumes = "storage.volumes"
    Usage = "usage"
    Concurrency = "settings.concurrency"


class WorkspaceChangeType(StringEnum):
    Created = "created"
    Updated = "updated"
    Deleted = "deleted"


class WorkspaceChangeEvent(HttpModel):
    event_id: str
    occurred_at: datetime
    workspace_id: str
    topic: WorkspaceChangeTopic
    change: WorkspaceChangeType
    resource_id: str
    app_id: str | None = None
    deployment_id: str | None = None
    stub_id: str | None = None
    task_id: str | None = None
    root_task_id: str | None = None
    container_id: str | None = None

    @field_validator("event_id", "workspace_id", "resource_id")
    @classmethod
    def _required_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be empty")
        return normalized

    @field_validator(
        "app_id",
        "deployment_id",
        "stub_id",
        "task_id",
        "root_task_id",
        "container_id",
    )
    @classmethod
    def _optional_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be empty")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


__all__ = [
    "WorkspaceChangeEvent",
    "WorkspaceChangeTopic",
    "WorkspaceChangeType",
]
