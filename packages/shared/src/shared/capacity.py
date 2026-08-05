from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum

TERMINAL_REASON_MAX_LENGTH = 500


class CapacityFailureCode(StringEnum):
    """Typed diagnosis for a capacity acquisition or sizing failure."""

    ProviderLaunchFailed = "provider_launch_failed"
    ProviderReconciliationFailed = "provider_reconciliation_failed"
    ProviderUnavailable = "provider_unavailable"
    CapacityPlanningFailed = "capacity_planning_failed"
    JoinAuthorityUnusable = "join_authority_unusable"
    PlacementFailed = "placement_failed"
    Unknown = "unknown"


_CAPACITY_FAILURE_DESCRIPTIONS: dict[CapacityFailureCode, str] = {
    CapacityFailureCode.ProviderLaunchFailed: "provider launch failed",
    CapacityFailureCode.ProviderReconciliationFailed: "provider reconciliation failed",
    CapacityFailureCode.ProviderUnavailable: "provider is unavailable",
    CapacityFailureCode.CapacityPlanningFailed: "capacity planning failed",
    CapacityFailureCode.JoinAuthorityUnusable: "provider join authority is no longer usable",
    CapacityFailureCode.PlacementFailed: "no compatible capacity could be placed",
    CapacityFailureCode.Unknown: "capacity operation failed",
}


def capacity_failure_message(code: CapacityFailureCode, *, exception_type: str = "") -> str:
    """Operator-safe description of a capacity failure.

    Upstream exception text can carry presigned URLs, tokens, and account
    identifiers, and it reaches both durable records and the user-visible task
    error. Only the typed code and the exception class name are safe to carry;
    the original exception belongs in logs, not in persisted or returned state.
    """
    description = _CAPACITY_FAILURE_DESCRIPTIONS[code]
    if exception_type:
        return f"{description} ({exception_type})"
    return description


def _bounded_terminal_reason(value: object) -> object:
    """Bound a terminal reason so an over-long diagnosis never fails persistence.

    Reasons are typed and operator-safe at their source; this stays as a
    last-resort guard on the durable column width.
    """
    if not isinstance(value, str) or len(value) <= TERMINAL_REASON_MAX_LENGTH:
        return value
    return value[: TERMINAL_REASON_MAX_LENGTH - 1] + "\u2026"


CAPACITY_OWNER_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class CapacityOwnerKind(StringEnum):
    WorkspaceAgent = "workspace_agent"
    ManagedPool = "managed_pool"
    PooledProvider = "pooled_provider"


class CapacityOwnerSource(StringEnum):
    Agent = "agent"
    Managed = "managed"
    Provider = "provider"


class CapacityAcquisitionStatus(StringEnum):
    ExistingPending = "existing_pending"
    Requested = "requested"
    AtLimit = "at_limit"
    TemporarilyUnavailable = "temporarily_unavailable"
    Unsupported = "unsupported"


class CapacityAcquisitionShape(ContractModel):
    cpu_millicores: int = Field(gt=0)
    memory_mib: int = Field(gt=0)
    gpu_type: str = Field(default="", max_length=160)
    gpu_count: int = Field(default=0, ge=0)
    runtime: str = Field(default="runc", min_length=1, max_length=80)
    preemptible: bool = False

    @model_validator(mode="after")
    def validate_gpu(self) -> CapacityAcquisitionShape:
        if (self.gpu_type == "") != (self.gpu_count == 0):
            raise ValueError("capacity acquisition GPU type and count must be configured together")
        if self.runtime != self.runtime.strip():
            raise ValueError("capacity acquisition runtime must be normalized")
        return self


class CapacityAcquisitionRequest(ContractModel):
    """What a reservation asks for. The unit it resolves to is compute's answer."""

    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    reservation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    operation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    shape: CapacityAcquisitionShape


class CapacityReleaseRequest(ContractModel):
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    reservation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    operation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)


class CapacityAcquisitionResult(ContractModel):
    status: CapacityAcquisitionStatus
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    reservation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    desired_unit: int = Field(ge=1)
    target_machine_id: str | None = None
    failure_code: CapacityFailureCode | None = None
    reason: str = Field(default="", max_length=TERMINAL_REASON_MAX_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def bound_reason(cls, value: object) -> object:
        return _bounded_terminal_reason(value)


_OWNER_SOURCES: dict[CapacityOwnerKind, CapacityOwnerSource] = {
    CapacityOwnerKind.WorkspaceAgent: CapacityOwnerSource.Agent,
    CapacityOwnerKind.ManagedPool: CapacityOwnerSource.Managed,
    CapacityOwnerKind.PooledProvider: CapacityOwnerSource.Provider,
}


def new_capacity_owner_id() -> str:
    return str(uuid4())


class CapacityOwnerIdentity(ContractModel):
    capacity_owner_id: str = Field(
        default_factory=new_capacity_owner_id,
        pattern=CAPACITY_OWNER_ID_PATTERN,
        frozen=True,
    )
    capacity_owner_kind: CapacityOwnerKind = Field(
        default=CapacityOwnerKind.WorkspaceAgent,
        frozen=True,
    )
    capacity_owner_source: CapacityOwnerSource = Field(
        default=CapacityOwnerSource.Agent,
        frozen=True,
    )

    @model_validator(mode="after")
    def validate_owner_source(self) -> CapacityOwnerIdentity:
        expected = _OWNER_SOURCES[self.capacity_owner_kind]
        if self.capacity_owner_source is not expected:
            raise ValueError(
                f"capacity owner kind {self.capacity_owner_kind.value!r} "
                f"requires source {expected.value!r}"
            )
        return self


class CapacityPoolPolicy(ContractModel):
    initial_workers: int = Field(default=0, ge=0)
    min_workers: int = Field(default=0, ge=0)
    max_workers: int = Field(default=1, ge=0)
    scaling_enabled: bool = False
    default_eligible: bool = False
    priority: int = Field(default=0, ge=-(2**31), le=2**31 - 1)
    min_free_cpu_millicores: int = Field(default=0, ge=0)
    min_free_memory_mib: int = Field(default=0, ge=0)
    min_free_gpu_count: int = Field(default=0, ge=0)
    worker_cpu_millicores: int = Field(default=0, ge=0)
    worker_memory_mib: int = Field(default=0, ge=0)
    worker_gpu_type: str = Field(default="", max_length=160)
    worker_gpu_count: int = Field(default=0, ge=0)
    worker_runtimes: tuple[str, ...] = ("runc",)
    worker_preemptible: bool = False
    idle_drain_timeout_seconds: int = Field(default=300, ge=60, le=86_400)
    scale_up_cooldown_seconds: int = Field(default=5, ge=0, le=86_400)
    scale_down_cooldown_seconds: int = Field(default=60, ge=0, le=86_400)
    registration_timeout_seconds: int = Field(default=600, ge=30, le=3_600)

    @model_validator(mode="after")
    def validate_policy(self) -> CapacityPoolPolicy:
        if not self.min_workers <= self.initial_workers <= self.max_workers:
            raise ValueError("capacity policy must satisfy min <= initial <= max")
        if (self.worker_gpu_type == "") != (self.worker_gpu_count == 0):
            raise ValueError("worker GPU type and count must be configured together")
        if not self.worker_runtimes:
            raise ValueError("capacity policy requires at least one worker runtime")
        normalized_runtimes = _unique_nonempty(self.worker_runtimes)
        if len(normalized_runtimes) != len(self.worker_runtimes):
            raise ValueError("worker runtimes must be non-empty and unique")
        if self.scaling_enabled and (
            self.max_workers == 0 or self.worker_cpu_millicores == 0 or self.worker_memory_mib == 0
        ):
            raise ValueError(
                "enabled capacity scaling requires a positive maximum, worker CPU, and memory"
            )
        has_free_capacity_target = any(
            (
                self.min_free_cpu_millicores,
                self.min_free_memory_mib,
                self.min_free_gpu_count,
            )
        )
        if has_free_capacity_target and not self.scaling_enabled:
            raise ValueError("minimum free capacity requires scaling to be enabled")
        if self.min_free_gpu_count > 0 and (not self.worker_gpu_type or self.worker_gpu_count == 0):
            raise ValueError("minimum free GPU capacity requires a GPU worker shape")
        return self


class CapacityPoolSizingSnapshot(ContractModel):
    """What a pool's size is and has been, read from the two owners of that fact.

    Nothing here is stored under its own name. `desired_units` is the provider's
    own count; every other field is computed from the pool's
    `compute_capacity_operations` rows on each read. That is what makes the
    scale-up backoff survive a restart: a pool whose launches keep failing
    resumes the interval its recorded failures earned instead of starting over
    at zero and buying another machine a tick later.
    """

    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    desired_units: int = Field(default=0, ge=0)
    peak_desired_units: int = Field(default=0, ge=0)
    pending_operation_id: str = Field(default="", max_length=80)
    pending_desired_units: int = Field(default=0, ge=0)
    last_requested_at: datetime | None = None
    last_released_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_failure_at: datetime | None = None


def capacity_owner_for_provider(provider: str) -> tuple[CapacityOwnerKind, CapacityOwnerSource]:
    normalized = provider.strip().lower()
    if normalized in {"", "agent", "local"}:
        return CapacityOwnerKind.WorkspaceAgent, CapacityOwnerSource.Agent
    if normalized == CapacityOwnerSource.Managed.value:
        return CapacityOwnerKind.ManagedPool, CapacityOwnerSource.Managed
    return CapacityOwnerKind.PooledProvider, CapacityOwnerSource.Provider


def _unique_nonempty(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    return tuple(dict.fromkeys(value for value in normalized if value))


__all__ = [
    "CAPACITY_OWNER_ID_PATTERN",
    "CapacityAcquisitionRequest",
    "CapacityAcquisitionResult",
    "CapacityAcquisitionShape",
    "CapacityAcquisitionStatus",
    "CapacityOwnerIdentity",
    "CapacityOwnerKind",
    "CapacityOwnerSource",
    "CapacityPoolPolicy",
    "CapacityPoolSizingSnapshot",
    "CapacityReleaseRequest",
    "capacity_owner_for_provider",
    "new_capacity_owner_id",
]
