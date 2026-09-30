from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.compute_enrollment import MachineBootstrapFailureReason
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


class MachineLifecycle(StringEnum):
    """Where a machine is in its life, from the request that made it to its removal.

    One phase for every machine, however it arrived: a host an account joined
    passes through `requested`, `joining` and `ready`; a node a connected cloud
    launched adds `provisioning` and `booting` in between. The phase never
    describes whether the agent is reachable right now; that is `connected` on
    the view, and a machine that stops reporting stays `ready` with a message
    saying so.
    """

    Requested = "requested"
    Provisioning = "provisioning"
    Booting = "booting"
    Joining = "joining"
    Ready = "ready"
    Draining = "draining"
    Stopping = "stopping"
    Stopped = "stopped"
    Resuming = "resuming"
    Terminating = "terminating"
    Deleted = "deleted"
    Failed = "failed"


LIVE_MACHINE_LIFECYCLES = frozenset(
    {
        MachineLifecycle.Requested,
        MachineLifecycle.Provisioning,
        MachineLifecycle.Booting,
        MachineLifecycle.Joining,
        MachineLifecycle.Ready,
        MachineLifecycle.Draining,
        MachineLifecycle.Stopping,
        MachineLifecycle.Stopped,
        MachineLifecycle.Resuming,
        MachineLifecycle.Terminating,
    }
)
"""Phases a machine is still shown in. `Failed` and `Deleted` are the two ends."""

PENDING_MACHINE_LIFECYCLES = frozenset(
    {
        MachineLifecycle.Requested,
        MachineLifecycle.Provisioning,
        MachineLifecycle.Booting,
        MachineLifecycle.Joining,
        MachineLifecycle.Resuming,
    }
)
"""Phases awaiting readiness for the current machine start."""


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
    """Kept in step with `lifecycle` by the lifecycle writer; never exposed."""
    lifecycle: MachineLifecycle = MachineLifecycle.Requested
    lifecycle_message: str = ""
    lifecycle_failure: MachineBootstrapFailureReason | None = None
    lifecycle_at: datetime = Field(default_factory=utc_now)
    """When the machine entered its current phase."""
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
    "LIVE_MACHINE_LIFECYCLES",
    "PENDING_MACHINE_LIFECYCLES",
    "AgentLease",
    "AgentRecord",
    "LeaseStatus",
    "Machine",
    "MachineLifecycle",
    "ResourceStatus",
    "Worker",
]
