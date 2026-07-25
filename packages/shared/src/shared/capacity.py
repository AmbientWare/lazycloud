from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

TERMINAL_REASON_MAX_LENGTH = 500


def _bounded_terminal_reason(value: object) -> object:
    """Bound provider failure text so a long reason never fails persistence.

    Terminal reasons carry upstream error text of unbounded length; the sizing
    state must record a diagnosis rather than reject it and lose the failure.
    """
    if not isinstance(value, str) or len(value) <= TERMINAL_REASON_MAX_LENGTH:
        return value
    return value[: TERMINAL_REASON_MAX_LENGTH - 1] + "\u2026"


CAPACITY_OWNER_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class CapacityOwnerKind(StringEnum):
    GlobalKubernetesDeployment = "global_kubernetes_deployment"
    WorkspaceAgent = "workspace_agent"
    ManagedPool = "managed_pool"
    PooledProvider = "pooled_provider"


class CapacityOwnerSource(StringEnum):
    Kubernetes = "kubernetes"
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
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    reservation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    operation_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    desired_unit: int = Field(ge=1)
    shape: CapacityAcquisitionShape


class CapacityAcquisitionPlanningRequest(ContractModel):
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
    reason: str = ""


_OWNER_SOURCES: dict[CapacityOwnerKind, CapacityOwnerSource] = {
    CapacityOwnerKind.GlobalKubernetesDeployment: CapacityOwnerSource.Kubernetes,
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


class CapacityPoolSizingState(ContractModel):
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    pool_name: str
    workspace_id: str
    revision: int = Field(default=0, ge=0)
    initial_target_reached: bool = False
    operation_id: str = Field(default="", max_length=80)
    target_units: int = Field(default=0, ge=0)
    operation_started_at: datetime | None = None
    last_scale_up_at: datetime | None = None
    last_scale_down_at: datetime | None = None
    retry_after_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    terminal_reason: str = Field(default="", max_length=TERMINAL_REASON_MAX_LENGTH)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("terminal_reason", mode="before")
    @classmethod
    def bound_terminal_reason(cls, value: object) -> object:
        return _bounded_terminal_reason(value)


class CapacityPoolSizingStateUpdate(ContractModel):
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    expected_revision: int = Field(ge=0)
    initial_target_reached: bool = False
    operation_id: str = Field(default="", max_length=80)
    target_units: int = Field(default=0, ge=0)
    operation_started_at: datetime | None = None
    last_scale_up_at: datetime | None = None
    last_scale_down_at: datetime | None = None
    retry_after_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    terminal_reason: str = Field(default="", max_length=TERMINAL_REASON_MAX_LENGTH)

    @field_validator("terminal_reason", mode="before")
    @classmethod
    def bound_terminal_reason(cls, value: object) -> object:
        return _bounded_terminal_reason(value)


def capacity_owner_for_provider(provider: str) -> tuple[CapacityOwnerKind, CapacityOwnerSource]:
    normalized = provider.strip().lower()
    if normalized == CapacityOwnerSource.Kubernetes.value:
        return (
            CapacityOwnerKind.GlobalKubernetesDeployment,
            CapacityOwnerSource.Kubernetes,
        )
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
    "CapacityAcquisitionPlanningRequest",
    "CapacityAcquisitionRequest",
    "CapacityAcquisitionResult",
    "CapacityAcquisitionShape",
    "CapacityAcquisitionStatus",
    "CapacityOwnerIdentity",
    "CapacityOwnerKind",
    "CapacityOwnerSource",
    "CapacityPoolPolicy",
    "CapacityPoolSizingState",
    "CapacityPoolSizingStateUpdate",
    "CapacityReleaseRequest",
    "capacity_owner_for_provider",
    "new_capacity_owner_id",
]
