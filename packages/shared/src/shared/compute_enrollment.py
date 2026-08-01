from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum


class ComputeCredentialStatus(StringEnum):
    Active = "active"
    Revoked = "revoked"


class ComputeMachineEnrollmentStatus(StringEnum):
    Active = "active"
    Revoked = "revoked"
    Deleted = "deleted"


class AgentCapacityState(StringEnum):
    Available = "available"
    Preempting = "preempting"
    Cordoned = "cordoned"


class TailnetEnrollmentPhase(StringEnum):
    Unconfigured = "unconfigured"
    Rotating = "rotating"
    AwaitingDevice = "awaiting_device"
    Bound = "bound"
    Failed = "failed"
    Revoked = "revoked"


class MachineReadinessPhase(StringEnum):
    Joining = "joining"
    Ready = "ready"
    Blocked = "blocked"
    Offline = "offline"
    Revoked = "revoked"


class MachineBootstrapPhase(StringEnum):
    """How far the node itself reports having got.

    Progress a node can observe about itself. Whether it serves workloads is
    not one of those things — that is `MachineServiceState`, derived from the
    scheduler record.
    """

    Requested = "requested"
    Provisioning = "provisioning"
    Booting = "booting"
    Joining = "joining"
    Failed = "failed"
    Deleting = "deleting"


class MachineServiceState(StringEnum):
    """What the platform concludes about a machine, never what it claims."""

    Provisioning = "provisioning"
    Joining = "joining"
    Serving = "serving"
    Degraded = "degraded"
    Failed = "failed"
    Deleting = "deleting"


class MachineBootstrapFailureReason(StringEnum):
    AgentDownloadFailed = "agent_download_failed"
    RuntimeInstallFailed = "runtime_install_failed"
    NetworkJoinFailed = "network_join_failed"
    ProviderIdentityFailed = "provider_identity_failed"
    AgentEnrollmentFailed = "agent_enrollment_failed"
    WorkerStartFailed = "worker_start_failed"
    WorkerReadinessFailed = "worker_readiness_failed"
    BootstrapTimedOut = "bootstrap_timed_out"
    ProviderStopped = "provider_stopped"
    ProviderTerminated = "provider_terminated"
    Unknown = "unknown"


class TailnetCleanupTombstone(ContractModel):
    id: str
    workspace_id: str
    pool_name: str
    machine_id: str
    generations: list[int] = Field(default_factory=list)
    auth_key_ids: list[str] = Field(default_factory=list)
    device_ids: list[str] = Field(default_factory=list)
    not_before: datetime
    next_attempt_at: datetime
    revision: int = Field(default=1, ge=1)
    attempt_count: int = Field(default=0, ge=0)
    last_error: str = ""
    claim_token: str = ""
    claimed_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PoolBootstrapCredential(ContractModel):
    """The tailnet key a pool's launch template hands every node it starts.

    `auth_key` is a plain string rather than `SecretStr` because repositories
    persist a record through `model_dump_json`, which would write the mask
    instead of the key. `repr=False` keeps it out of tracebacks and logs; what
    keeps it out of API responses is that this record has no route.

    `superseded_auth_key_ids` holds keys a refresh replaced but whose grace
    period has not elapsed: an instance launched from the previous launch-template
    version is still booting with one.
    """

    id: str
    workspace_id: str
    pool_id: str
    pool_name: str
    tag: str
    auth_key_id: str
    auth_key: str = Field(repr=False)
    expires_at: datetime
    superseded_auth_key_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


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


__all__ = [
    "AgentCapacityState",
    "ComputeCredentialStatus",
    "ComputeMachineEnrollmentStatus",
    "ComputePreflightCheck",
    "MachineBootstrapFailureReason",
    "MachineBootstrapPhase",
    "MachineReadinessPhase",
    "PoolBootstrapCredential",
    "PreflightSeverity",
    "TailnetCleanupTombstone",
    "TailnetEnrollmentPhase",
]
