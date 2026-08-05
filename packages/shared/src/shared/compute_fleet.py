from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.capacity import CapacityOwnerIdentity, CapacityPoolPolicy
from shared.contracts import ContractModel
from shared.enums import StringEnum
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


class Pool(CapacityOwnerIdentity, CapacityPoolPolicy):
    name: str
    provider: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Machine(ContractModel):
    id: str
    pool: str = "default"
    provider: str = "local"
    status: ResourceStatus = ResourceStatus.Created
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    address: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Worker(ContractModel):
    id: str
    machine_id: str | None = None
    pool: str = "default"
    status: ResourceStatus = ResourceStatus.Created
    labels: dict[str, str] = Field(default_factory=dict)
    last_seen_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class AgentRecord(ContractModel):
    id: str
    name: str
    pool: str = "default"
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
    "Pool",
    "ResourceStatus",
    "Worker",
]
