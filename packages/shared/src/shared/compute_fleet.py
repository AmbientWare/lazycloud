from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.placement import Placement
from shared.timestamps import utc_now


class ResourceStatus(StringEnum):
    Created = "created"
    Running = "running"
    Stopped = "stopped"
    Failed = "failed"
    Deleted = "deleted"


class LeaseStatus(StringEnum):
    Active = "active"
    Released = "released"
    Expired = "expired"


class Machine(ContractModel):
    id: str
    name: str = ""
    """Account-unique name a workload pins to; empty for provider-bought machines."""
    workspace_ids: tuple[str, ...] = ()
    """Workspaces whose workloads may land here. Empty for platform and provider capacity."""
    placement: Placement = Placement.platform()
    capacity_owner_id: str = ""
    """Unit that bought this machine, from the join credential it enrolled with."""
    provider: str = "local"
    status: ResourceStatus = ResourceStatus.Created
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    address: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Worker(ContractModel):
    id: str
    machine_id: str | None = None
    placement: Placement = Placement.platform()
    status: ResourceStatus = ResourceStatus.Created
    labels: dict[str, str] = Field(default_factory=dict)
    last_seen_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class AgentRecord(ContractModel):
    id: str
    name: str
    placement: Placement = Placement.platform()
    status: ResourceStatus = ResourceStatus.Created
    version: str = "local"
    capacity: dict[str, int | float | str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    install_command: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentLease(ContractModel):
    id: str
    agent_id: str
    resource_type: str
    resource_id: str
    status: LeaseStatus = LeaseStatus.Active
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None


__all__ = [
    "AgentLease",
    "AgentRecord",
    "LeaseStatus",
    "Machine",
    "ResourceStatus",
    "Worker",
]
