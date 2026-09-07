from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import Field, JsonValue, model_validator

from shared.capacity import CapacityOwnerIdentity, CapacityOwnerKind, MachinePool, UnitName
from shared.container_requests import OciRuntimeName
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.routing import BackendRouteTransport, PrivateUnitFallback
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit
from shared.timestamps import utc_now

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


LAZYCLOUD_MACHINE_POOL = "lazycloud"
"""Pool the platform's own fleet stamps on its machines."""


class ComputeCapacityMode(StringEnum):
    Direct = "direct"
    Pooled = "pooled"


class ComputeUnitVisibility(StringEnum):
    Public = "public"
    Internal = "internal"


class ComputeUnitPhase(StringEnum):
    Provisioning = "provisioning"
    Ready = "ready"
    Updating = "updating"
    Degraded = "degraded"
    Deleting = "deleting"
    Deleted = "deleted"
    Failed = "failed"


ENDED_UNIT_PHASES = frozenset({ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted})
"""Phases a unit only reaches because something asked it to stop.

Every other phase describes capacity the unit is still meant to hold, which is
what makes this the answer to "was this unit torn down, or has it not been built
yet" — a question a provider cannot answer, because both look like an account
with no autoscaling group in it.
"""


class ComputeResourceRequirements(ContractModel):
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mb: int = Field(default=0, ge=0)
    gpu: list[str] = Field(default_factory=list)
    gpu_count: int = Field(default=0, ge=0)
    architecture: str = Field(default="", max_length=64)
    runtime: str = Field(default=OciRuntimeName.Runsc.value, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_gpu(self) -> ComputeResourceRequirements:
        if bool(self.gpu) is (self.gpu_count > 0):
            return self
        raise ValueError("GPU models and count must be requested together")


class WorkspaceComputePolicy(ContractModel):
    """The scheduling decisions a workspace makes for itself.

    Provisioning limits and defaults are not among them: those belong to the
    connected account that provides the capacity, one configuration for every
    workspace that account backs.
    """

    id: str = Field(pattern=_UUID_PATTERN)
    workspace_id: str = Field(pattern=_UUID_PATTERN)
    revision: int = Field(default=1, ge=1)
    default_pool: MachinePool = Field(
        default=MachinePool(LAZYCLOUD_MACHINE_POOL), min_length=1, max_length=240
    )
    """Pool workloads land in when they name none."""
    created_at: datetime
    updated_at: datetime


class ComputeUnitProviderState(ContractModel):
    resource_id: str = Field(default="", max_length=2048)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    degraded_reason: str | None = Field(default=None, min_length=1, max_length=512)
    """Durable reason the control plane stopped restoring pool capacity.

    Set when bootstrap relaunch attempts are exhausted; cleared by an explicit
    capacity mutation (public scale or an account compute configuration update)
    or, on its own, once the relaunch interval has passed since `degraded_at`.
    """
    degraded_at: datetime | None = None
    """When the reason above was recorded, so a relaunch can be paced from it."""
    launch_attempt_baseline: int = Field(default=0, ge=0)
    """Attempt ordinal the current failure streak counts from.

    `launch_attempt` is a per-pool ordinal that only ever increases, so
    comparing it directly against the relaunch limit means a pool that once
    exhausted its attempts stays exhausted for every machine it launches
    afterwards. Recovery raises this to the ordinal reached at that moment, so
    the limit measures the streak since recovery rather than the pool's whole
    history.
    """


class ComputeUnitRecord(CapacityOwnerIdentity):
    """One provisioning unit: a single source of machines a workspace draws on.

    A provider unit has identity `(workspace_id, provider_ref, region,
    capability_key, root_volume_gib)`. An AWS unit owns one Auto Scaling group
    and launch template; a Hetzner unit owns a labeled group of servers. The
    `agent` and `local` units own no provider resources but still carry a worker
    shape and a scaling policy, because worker admission, sizing and drain all
    key on the owning unit.

    `machine_pool` is the pool this unit stamps on every machine it
    produces. Several units may name one pool; a unit names exactly one.
    """

    id: str = Field(pattern=_UUID_PATTERN)
    workspace_id: str = Field(pattern=_UUID_PATTERN)
    name: UnitName = Field(min_length=1, max_length=240)
    pool: MachinePool = Field(min_length=1, max_length=240)
    provider: str = Field(default="local", min_length=1, max_length=120)
    selector: str = Field(default="", max_length=255)
    status: str = Field(default=ComputeUnitPhase.Ready.value, max_length=80)
    source: str = Field(default="autosolver", max_length=80)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    expires_at: datetime | None = None
    provider_ref: str = Field(default="", max_length=160)
    provider_connection_id: str | None = Field(default=None, pattern=_UUID_PATTERN)
    platform_fleet: bool = False
    """Whether this unit is the platform's own capacity rather than a customer's.

    Copied from the resolved provider policy beside the pool, in the same statement and from
    the same source, so a consumer reads one record to know both what this
    capacity is called and whom it serves."""
    capacity_mode: ComputeCapacityMode = ComputeCapacityMode.Direct
    visibility: ComputeUnitVisibility = ComputeUnitVisibility.Public
    region: str = Field(default="", max_length=64)
    offer_id: str = Field(default="", max_length=255)
    capability_key: str = Field(default="", max_length=255)
    offer_cost_terms: SupplierCostTerms | None = None
    offer_storage_mib: int | None = Field(default=None, ge=0)
    supplier_cpu_unit: SupplierCpuUnit = SupplierCpuUnit.Unknown
    supplier_cpu_count: int | None = Field(default=None, ge=0)
    desired_machines: int = Field(default=0, ge=0)
    initial_machines: int = Field(default=0, ge=0)
    min_machines: int = Field(default=0, ge=0)
    max_machines: int = Field(default=0, ge=0)
    observed_machines: int = Field(default=0, ge=0)
    replacement_machine_id: str = Field(default="", max_length=160)
    """Superseded machine paired with one provider-side surge.

    The surge is operational capacity and is deliberately not included in
    ``desired_machines``. Keeping the pair durable lets a restarted controller
    finish one replacement without interpreting ordinary desired capacity as
    permission to shrink the pool.
    """
    replacement_template_version: str = Field(default="", max_length=160)
    worker_rollout_surge: bool = False
    """One temporary host preserving serving capacity during worker image updates."""
    generation: int = Field(default=1, ge=1)
    phase: ComputeUnitPhase = ComputeUnitPhase.Ready
    provider_state: ComputeUnitProviderState = Field(default_factory=ComputeUnitProviderState)
    scaling_enabled: bool = False
    default_eligible: bool = False
    priority: int = Field(default=0, ge=-(2**31), le=2**31 - 1)
    """Preference for this unit over another that could serve the same work.

    Higher is preferred, in the order capacity is acquired and in the choice of
    which existing worker a request lands on. The polarity is stated because it
    was once opposite in two sorts ten lines apart, and a number whose direction
    nobody wrote down is a number the next reader has to guess.

    A tier rather than a weight: work fills the highest tier that fits before any
    of the next, and inside a tier placement is unchanged. It cannot be tuned to
    a value that stops capacity packing, which is what lets an idle pool drain.
    """
    min_free_cpu_millicores: int = Field(default=0, ge=0)
    min_free_memory_mib: int = Field(default=0, ge=0)
    min_free_gpu_count: int = Field(default=0, ge=0)
    worker_cpu_millicores: int = Field(default=0, ge=0)
    worker_memory_mib: int = Field(default=0, ge=0)
    worker_gpu_type: str = Field(default="", max_length=160)
    worker_gpu_count: int = Field(default=0, ge=0)
    worker_runtimes: tuple[str, ...] = (OciRuntimeName.Runsc.value,)
    worker_preemptible: bool = False
    idle_drain_timeout_seconds: int = Field(default=300, ge=60, le=86_400)
    scale_up_cooldown_seconds: int = Field(default=5, ge=0, le=86_400)
    scale_down_cooldown_seconds: int = Field(default=60, ge=0, le=86_400)
    registration_timeout_seconds: int = Field(default=600, ge=30, le=3_600)
    root_volume_gib: int = Field(default=200, ge=50, le=2048)
    transport: BackendRouteTransport = BackendRouteTransport.PrivateNetwork
    fallback: PrivateUnitFallback = PrivateUnitFallback.Internal
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_capacity(self) -> ComputeUnitRecord:
        if not self.min_machines <= self.desired_machines <= self.max_machines:
            raise ValueError("compute pool capacity must satisfy min <= desired <= max")
        if not self.min_machines <= self.initial_machines <= self.max_machines:
            raise ValueError("compute pool capacity must satisfy min <= initial <= max")
        internal = self.visibility is ComputeUnitVisibility.Internal
        if internal and (
            not self.provider_ref
            or not self.region
            or not self.offer_id
            or not self.capability_key
            or self.capacity_mode is not ComputeCapacityMode.Pooled
        ):
            raise ValueError("internal provider pools require complete placement identity")
        if internal and self.capacity_owner_kind is not CapacityOwnerKind.PooledProvider:
            raise ValueError("internal provider pools require pooled-provider capacity ownership")
        if internal and self.capacity_owner_id != self.id:
            raise ValueError("internal provider pool ID must own its capacity")
        if internal and self.provider_connection_id is None and not self.platform_fleet:
            raise ValueError("customer provider pools require a connected account")
        if not internal and self.provider_connection_id is not None:
            raise ValueError("public pools cannot own a provider connection")
        return self

    @model_validator(mode="after")
    def validate_worker_shape(self) -> ComputeUnitRecord:
        if (self.worker_gpu_type == "") != (self.worker_gpu_count == 0):
            raise ValueError("worker GPU type and count must be configured together")
        if not self.worker_runtimes:
            raise ValueError("compute pool requires at least one worker runtime")
        normalized_runtimes = _unique_nonempty(self.worker_runtimes)
        if len(normalized_runtimes) != len(self.worker_runtimes):
            raise ValueError("worker runtimes must be non-empty and unique")
        if self.scaling_enabled and (
            self.max_machines == 0 or self.worker_cpu_millicores == 0 or self.worker_memory_mib == 0
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


def _unique_nonempty(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    return tuple(dict.fromkeys(value for value in normalized if value))


__all__ = [
    "LAZYCLOUD_MACHINE_POOL",
    "ComputeCapacityMode",
    "ComputeResourceRequirements",
    "ComputeUnitPhase",
    "ComputeUnitProviderState",
    "ComputeUnitRecord",
    "ComputeUnitVisibility",
    "MachinePool",
    "UnitName",
    "WorkspaceComputePolicy",
]
