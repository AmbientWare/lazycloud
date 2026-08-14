from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import Field, JsonValue, field_validator

from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_policy import MachinePool
from shared.container_requests import OciRuntimeName
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.routing import AgentBackendRoute
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner

DEFAULT_CONTAINER_STATE_TTL_SECONDS = 900


class SchedulerWorkerStatus(StringEnum):
    Pending = "pending"
    Available = "available"
    Draining = "draining"
    Unavailable = "disabled"


class SchedulerContainerStatus(StringEnum):
    Pending = "pending"
    Running = "running"
    Stopping = "stopping"
    Complete = "complete"
    Failed = "failed"


class WorkerCapacityChange(StringEnum):
    Add = "add"
    Remove = "remove"


class NetworkIpMutationAction(StringEnum):
    Set = "set"
    Remove = "remove"
    Move = "move"
    Noop = "noop"
    Reject = "reject"


class WorkerRepositoryLockKind(StringEnum):
    Worker = "worker"
    Container = "container"
    ImagePull = "image-pull"
    Network = "network"
    WorkspaceConcurrency = "workspace-concurrency"


class SchedulerContainerSubmitStatus(StringEnum):
    Queued = "queued"
    Error = "error"


class SchedulerWorkerRequest(ContractModel):
    workspace_id: str
    stub_id: str
    deployment_id: str = ""
    container_id: str
    cpu_millicores: int = 0
    memory_mib: int = 0
    gpu_type: str = ""
    gpu_request: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    pool_selector: str = ""
    """Pool this request must land in, empty to take the default.

    A group may be fed by several units, so the request names the pool and the
    capacity controllers arbitrate which unit serves it. Naming the unit here
    would pin the request to one candidate and suppress failover.
    """
    architecture: str = "amd64"
    provider_runtime: str = OciRuntimeName.Runsc.value
    runtime_class: str = ""
    docker_enabled: bool = False
    preemptible: bool = False
    gpu_limit: int = 0
    cpu_limit_millicores: int = 0
    retry_count: int = 0
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator(
        "cpu_millicores",
        "memory_mib",
        "gpu_count",
        "gpu_limit",
        "cpu_limit_millicores",
        "retry_count",
    )
    @classmethod
    def request_numbers_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "request values cannot be negative"
            raise ValueError(msg)
        return value

    def requeued(self, *, now: datetime | None = None) -> SchedulerWorkerRequest:
        return self.model_copy(
            update={"retry_count": self.retry_count + 1, "timestamp": now or utc_now()}
        )


class WorkerUnavailableReason(StringEnum):
    """Typed diagnosis for a worker leaving the schedulable set."""

    RegistrationFailed = "registration_failed"
    ReadinessValidationFailed = "readiness_validation_failed"
    SourceCacheUnavailable = "source_cache_unavailable"
    Draining = "draining"
    MachineRetired = "machine_retired"
    AgentDisconnected = "agent_disconnected"
    OperatorCordon = "operator_cordon"
    ShuttingDown = "shutting_down"


def worker_serves_owner(
    *,
    private_worker: bool,
    worker_owner_user_id: str,
    request_owner_user_id: str,
) -> bool:
    """Whether a worker may run a request belonging to `request_owner_user_id`.

    The platform fleet is shared, which is what lets any workspace place work on it.
    A private worker is the account's own machine, so it serves every workspace that
    account owns—the customer's data is on both sides of that boundary, and asking
    them to connect the same hardware once per workspace answered nothing.

    Accept the consequence deliberately: between an owner's own workspaces, private
    capacity is no longer a hard isolation boundary, so a container escape on their
    machine reaches their other environments. It stops there. Platform-managed
    capacity is a different rule above, and no comparison here can widen it.

    A private record naming no owner was written without the authority to name one,
    so it serves none rather than all.
    """

    if not private_worker:
        return True
    return bool(worker_owner_user_id) and worker_owner_user_id == request_owner_user_id


class SchedulerWorkerRecord(ContractModel):
    worker_id: str
    pool: MachinePool
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    workspace_id: str = ""
    """Workspace that enrolled a private worker; empty on the shared platform fleet.

    Written from the authority that decides a worker's tenant—the machine's enrollment
    for an agent, the presented token at registration—never from what a worker says
    about itself. Records which workspace brought the machine in; it is not what
    placement compares.
    """

    owner_user_id: str = ""
    """Account whose machine this is, and the whole of the private-placement rule.

    Stamped from the same authority as `workspace_id`. Compared rather than the
    workspace because one account's capacity serves every workspace it owns.
    """

    billing_owner: UsageBillingOwner = UsageBillingOwner.PlatformFleet
    """Who pays for what runs here, decided by the control plane at registration.

    Stamped from the same authority as `workspace_id` and `owner_user_id`, and for
    the same reason: a worker runs on hardware a customer may hold root on, so a
    worker that named its own billing owner could mark its compute self-hosted and
    have it dropped from every bill. The default is the fleet because a worker the
    platform started is the only kind that arrives without an enrolling unit.
    """

    machine_id: str = ""
    status: SchedulerWorkerStatus = SchedulerWorkerStatus.Pending
    unavailable_reason: WorkerUnavailableReason | None = None
    unavailable_detail: str = Field(default="", max_length=512)
    gpu_type: str = ""
    runtime_class: str = ""
    runtime_classes: list[str] = Field(default_factory=list)
    private_worker: bool = False
    requires_pool_selector: bool = False
    preemptible: bool = False
    free_cpu_millicores: int = 0
    free_memory_mib: int = 0
    free_gpu_count: int = 0
    total_cpu_millicores: int = 0
    total_memory_mib: int = 0
    total_gpu_count: int = 0
    resource_version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator(
        "free_cpu_millicores",
        "free_memory_mib",
        "free_gpu_count",
        "total_cpu_millicores",
        "total_memory_mib",
        "total_gpu_count",
        "resource_version",
    )
    @classmethod
    def worker_numbers_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "worker values cannot be negative"
            raise ValueError(msg)
        return value

    def serves_owner(self, owner_user_id: str) -> bool:
        return worker_serves_owner(
            private_worker=self.private_worker,
            worker_owner_user_id=self.owner_user_id,
            request_owner_user_id=owner_user_id,
        )


class SchedulerContainerState(ContractModel):
    container_id: str
    stub_id: str
    workspace_id: str
    worker_id: str = ""
    status: SchedulerContainerStatus = SchedulerContainerStatus.Pending
    scheduled_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    gpu_type: str = ""
    gpu_count: int = 0
    cpu_millicores: int = 0
    memory_mib: int = 0
    image_build_id: str = ""
    image_id: str = ""
    image_build_upload_capability: str = ""
    failure_reason: str = ""

    @field_validator("gpu_count", "cpu_millicores", "memory_mib")
    @classmethod
    def container_numbers_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "container resource values cannot be negative"
            raise ValueError(msg)
        return value


class SchedulerContainerAddress(ContractModel):
    container_id: str
    address: str = ""
    route: AgentBackendRoute | None = None


class SchedulerContainerAddressMap(ContractModel):
    container_id: str
    address_map: dict[int, str] = Field(default_factory=dict)
    routes: list[AgentBackendRoute] = Field(default_factory=list)


class WorkerCapacityPlan(ContractModel):
    worker: SchedulerWorkerRecord
    change: WorkerCapacityChange
    request: SchedulerWorkerRequest
    accepted: bool
    reason: str = ""


class WorkerRemovalResult(ContractModel):
    worker_id: str
    removed: bool
    requeued_count: int = 0
    request_ids: list[str] = Field(default_factory=list)


class ContainerStatusUpdatePlan(ContractModel):
    container_id: str
    previous_status: SchedulerContainerStatus
    next_status: SchedulerContainerStatus
    changed: bool
    started_at_set: bool = False
    release_concurrency: bool = False
    ttl_seconds: int


class ContainerIpAssignment(ContractModel):
    container_id: str
    ip_address: str


class NetworkIpMutationPlan(ContractModel):
    action: NetworkIpMutationAction
    container_id: str
    ip_address: str = ""
    previous_ip_address: str = ""
    source_container_id: str = ""
    target_container_id: str = ""
    owner_container_id: str = ""
    changed: bool = False
    cleanup_previous_owner: bool = False
    cleanup_current_owner: bool = False
    reason: str = ""


class WorkerRepositoryLockRecord(ContractModel):
    kind: WorkerRepositoryLockKind
    key: str
    token: str = ""
    owner_id: str = ""
    resource_id: str = ""
    ttl_seconds: int
    retries: int = 0
    acquired: bool = True

    @field_validator("ttl_seconds", "retries")
    @classmethod
    def lock_values_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "lock values cannot be negative"
            raise ValueError(msg)
        return value


class WorkerRepositoryLockRelease(ContractModel):
    kind: WorkerRepositoryLockKind
    key: str
    token: str
    released: bool
    reason: str = ""


class SchedulerContainerSubmitResult(ContractModel):
    status: SchedulerContainerSubmitStatus
    container_id: str
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is SchedulerContainerSubmitStatus.Queued


class SchedulerContainerCancellationResult(ContractModel):
    container_id: str
    state_found: bool = False
    worker_id: str = ""
    pending_request_removed: bool = False
    worker_stop_required: bool = False


@runtime_checkable
class ContainerSchedulingDirectory(Protocol):
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None: ...

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap: ...


def gpu_count_for_capacity(
    gpu_type: str,
    gpu_request: list[str] | None,
    gpu_count: int,
) -> int:
    if gpu_type == "" and not gpu_request:
        return 0
    if gpu_count == 0:
        return 1
    return gpu_count
