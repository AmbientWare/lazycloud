from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.placement import Placement
from shared.routing import PrivateUnitFallback


class MachineStopPreparationReceipt(ContractModel):
    request_id: str = Field(min_length=1)
    cache_generation_id: str = ""
    cache_session_fence: int | None = Field(default=None, ge=1)


class ComputeCredentialStatus(StringEnum):
    Active = "active"
    Revoked = "revoked"


class ComputeMachineEnrollmentStatus(StringEnum):
    Active = "active"
    Revoked = "revoked"
    Deleted = "deleted"


class AgentCapacityState(StringEnum):
    Available = "available"
    Draining = "draining"
    Preempting = "preempting"
    Cordoned = "cordoned"


class AgentWorkerSlotStatus(StringEnum):
    Pending = "pending"
    Active = "active"
    Draining = "draining"
    Deleted = "deleted"


class MachineReadinessPhase(StringEnum):
    Joining = "joining"
    Ready = "ready"
    Blocked = "blocked"
    Offline = "offline"
    Revoked = "revoked"


class MachineBootstrapFailureReason(StringEnum):
    AgentDownloadFailed = "agent_download_failed"
    RuntimeInstallFailed = "runtime_install_failed"
    NetworkJoinFailed = "network_join_failed"
    ProviderIdentityFailed = "provider_identity_failed"
    AgentEnrollmentFailed = "agent_enrollment_failed"
    WorkerImagePullFailed = "worker_image_pull_failed"
    WorkerStartFailed = "worker_start_failed"
    WorkerReadinessFailed = "worker_readiness_failed"
    BootstrapTimedOut = "bootstrap_timed_out"
    HostPreflightFailed = "host_preflight_failed"
    """The host failed a required preflight check when its agent joined."""
    ServiceLost = "service_lost"
    """Served once, then stopped, and stayed stopped long enough to be believed.

    Distinct from `WorkerReadinessFailed`, which says a machine never got as far
    as taking work. Naming them apart is what keeps a fleet-wide outage from
    reading as a fleet of machines that each failed to start.
    """
    MachineRecordDeleted = "machine_record_deleted"
    """The machine row this instance enrolled as is gone.

    The provider instance still exists and still bills; only the join between
    them was severed, which reads locally as an instance that never enrolled.
    """
    ProviderStopped = "provider_stopped"
    ProviderTerminated = "provider_terminated"
    Unknown = "unknown"


class PreflightSeverity(StringEnum):
    Info = "info"
    Warning = "warning"
    Error = "error"


class ComputePreflightCheck(ContractModel):
    name: str
    ok: bool
    message: str = ""
    severity: PreflightSeverity = PreflightSeverity.Info
    remediation: str = ""

    @property
    def required(self) -> bool:
        return self.severity is PreflightSeverity.Error


DEFAULT_PRIVATE_EXECUTOR = "container"


class AgentBootstrapConfig(ContractModel):
    gateway_public_http_url: str
    gateway_grpc_host: str = ""
    gateway_grpc_port: int = 443
    gateway_grpc_tls: bool = True
    workspace_id: str
    placement: Placement
    executor: str = DEFAULT_PRIVATE_EXECUTOR
    fallback: PrivateUnitFallback = PrivateUnitFallback.Internal
    image_registry_store: str = ""
    image_clip_version: int = 2
    image_local_cache_enabled: bool = True


def agent_machine_worker_id(machine_id: str) -> str:
    """The id of a machine's worker, which the agent and the control plane each derive."""
    return str(uuid5(NAMESPACE_URL, f"agent-worker\x00{machine_id}"))


__all__ = [
    "DEFAULT_PRIVATE_EXECUTOR",
    "AgentBootstrapConfig",
    "AgentCapacityState",
    "AgentWorkerSlotStatus",
    "ComputeCredentialStatus",
    "ComputeMachineEnrollmentStatus",
    "ComputePreflightCheck",
    "MachineBootstrapFailureReason",
    "MachineReadinessPhase",
    "PreflightSeverity",
    "agent_machine_worker_id",
]
