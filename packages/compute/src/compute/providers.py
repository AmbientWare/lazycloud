from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, field_validator, model_validator
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    MachinePool,
    UnitName,
)
from shared.contracts import ContractModel
from shared.timestamps import to_utc
from shared.urls import normalize_http_origin

from compute.agent_control import MachineWorkerAvailability
from compute.offers import ComputeOffer


class ProviderMachineStatus:
    Pending = "pending"
    Active = "active"
    Terminated = "terminated"
    Unhealthy = "unhealthy"
    Unknown = "unknown"


def next_billing_renewal(
    *,
    started_at: datetime,
    minimum_seconds: int,
    quantum_seconds: int,
    now: datetime,
) -> datetime | None:
    if quantum_seconds == 0:
        return None
    first_boundary = to_utc(started_at) + timedelta(seconds=max(minimum_seconds, quantum_seconds))
    elapsed = (to_utc(now) - first_boundary).total_seconds()
    if elapsed < 0:
        return first_boundary
    return first_boundary + timedelta(
        seconds=(int(elapsed // quantum_seconds) + 1) * quantum_seconds
    )


class ProviderCapacityPhase(StrEnum):
    Provisioning = "provisioning"
    Ready = "ready"
    Degraded = "degraded"
    Deleting = "deleting"
    Deleted = "deleted"


class ProviderUnitBootstrap(ContractModel):
    control_plane_url: str
    enrollment_request_id: str
    agent_version: str
    agent_sha256: str
    agent_binary_url: str

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: str) -> str:
        return normalize_http_origin(value, field_name="control-plane URL")


class ProviderUnitRequest(ContractModel):
    workspace_id: str
    unit_id: str
    unit_name: UnitName
    provider_ref: str
    provider_connection_id: str | None
    generation: int
    offer: ComputeOffer
    desired_machines: int
    max_machines: int
    root_volume_gib: int = 200
    bootstrap: ProviderUnitBootstrap
    provider_state: ComputeUnitProviderState = Field(default_factory=ComputeUnitProviderState)

    @model_validator(mode="after")
    def validate_pooled_capacity(self) -> ProviderUnitRequest:
        if self.offer.capacity_mode is not ComputeCapacityMode.Pooled:
            raise ValueError("pooled capacity requires a pooled provider offer")
        if self.desired_machines < 0:
            raise ValueError("desired machines cannot be negative")
        if self.max_machines <= 0 or self.desired_machines > self.max_machines:
            raise ValueError("invalid pooled capacity bounds")
        if self.generation <= 0:
            raise ValueError("provider pool generation must be positive")
        return self


class ProviderUnitInstance(ContractModel):
    provider_instance_id: str
    status: str = ProviderMachineStatus.Unknown
    address: str = ""
    availability_zone: str = ""
    storage_volume_ids: tuple[str, ...] = ()
    # Version of the provider-side launch configuration this instance booted
    # with, empty when the provider reports none. A pool rolls its configuration
    # forward without disturbing running instances, so this is the only value
    # that identifies the release a node is actually on.
    booted_template_version: str = ""
    billing_started_at: datetime | None = None
    billing_minimum_seconds: int = Field(default=0, ge=0)
    billing_quantum_seconds: int = Field(default=0, ge=0)


class ResolvedProviderPolicy(ContractModel):
    workspace_id: str
    pool: MachinePool
    platform_fleet: bool
    default_region: str
    allowed_regions: tuple[str, ...]
    max_cpu_instances: int | None = Field(default=None, ge=0)
    max_gpu_instances: int | None = Field(default=None, ge=0)
    root_volume_gib: int = Field(default=200, ge=50, le=2048)
    idle_timeout_seconds: int = Field(default=300, ge=60, le=86_400)
    allowed_instance_types: tuple[str, ...] = ()
    hourly_cost_ceiling_micros: dict[str, int] = Field(default_factory=dict)
    warm_cpu_min: int = Field(default=0, ge=0)
    warm_cpu_max: int = Field(default=8, ge=0)
    warm_decrease_after_seconds: int = Field(default=600, ge=60, le=86_400)

    @model_validator(mode="after")
    def validate_policy(self) -> ResolvedProviderPolicy:
        if not self.workspace_id or not self.pool:
            raise ValueError("provider policy requires a capacity workspace and pool")
        if self.default_region not in self.allowed_regions:
            raise ValueError("provider default region must be allowed")
        if any(value <= 0 for value in self.hourly_cost_ceiling_micros.values()):
            raise ValueError("provider hourly cost ceilings must be positive")
        if self.warm_cpu_min > self.warm_cpu_max:
            raise ValueError("provider warm CPU minimum cannot exceed maximum")
        if self.max_cpu_instances is not None and self.warm_cpu_min > self.max_cpu_instances:
            raise ValueError("provider warm CPU minimum cannot exceed provider capacity limit")
        return self

    def machine_limit(self, *, gpu: bool) -> int | None:
        return self.max_gpu_instances if gpu else self.max_cpu_instances

    def accepts(self, offer: ComputeOffer) -> bool:
        ceiling = self.hourly_cost_ceiling_micros.get(offer.capability_key)
        return (
            offer.region in self.allowed_regions
            and (
                not self.allowed_instance_types
                or offer.instance_type in self.allowed_instance_types
            )
            and (
                (not self.platform_fleet and not self.hourly_cost_ceiling_micros)
                or (ceiling is not None and offer.hourly_cost_micros <= ceiling)
            )
        )


class ProviderUnitSnapshot(ContractModel):
    phase: ProviderCapacityPhase
    resource_id: str = ""
    desired_machines: int = 0
    max_machines: int = 0
    observed_machines: int = 0
    instances: list[ProviderUnitInstance] = Field(default_factory=list)
    provider_state: ComputeUnitProviderState = Field(default_factory=ComputeUnitProviderState)
    current_template_version: str = ""
    """Version an instance launched now would boot with, empty when unknown.

    The counterpart to each instance's `booted_template_version`: comparing the
    two is what says a node is running an older release than the pool would give
    it today, and without this the comparison can only be made inside the
    provider adapter.

    Empty means the provider cannot say, and must never be read as every instance
    being stale — a provider that reports no version is a provider whose nodes
    nothing should replace.
    """


class DirectMachineProvider(Protocol):
    def list_offers(self) -> Iterable[ComputeOffer]: ...

    def terminate_machine(self, provider_instance_id: str, /) -> None: ...

    def machine_storage_destroyed(
        self,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
        /,
    ) -> bool:
        """Return true only after an authoritative provider lookup proves absence."""
        ...


class DirectMachineProviderRegistry(Protocol):
    def snapshot(self, workspace: str) -> Mapping[str, DirectMachineProvider]: ...

    def snapshot_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> Mapping[str, DirectMachineProvider]: ...


class PooledCapacityProvider(Protocol):
    def list_offers(self) -> Iterable[ComputeOffer]: ...

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot: ...

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot: ...

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot: ...

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot: ...

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot: ...

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool: ...


class CapacityOwnerMutationLease(Protocol):
    """Serialize provider mutations and fence destructive changes from dispatch."""

    def mutation_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

    def dispatch_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class ResolvedComputeProvider:
    ref: str
    capacity_mode: ComputeCapacityMode
    connection_id: str | None = None
    direct: DirectMachineProvider | None = None
    pooled: PooledCapacityProvider | None = None
    policy: ResolvedProviderPolicy | None = None

    def __post_init__(self) -> None:
        if self.capacity_mode is ComputeCapacityMode.Direct and (
            self.direct is None or self.pooled is not None
        ):
            raise ValueError("direct provider resolution is invalid")
        if self.capacity_mode is ComputeCapacityMode.Pooled and (
            self.pooled is None or self.direct is not None or self.policy is None
        ):
            raise ValueError("pooled provider resolution is invalid")


class ComputeProviderResolver(Protocol):
    def list_platform_providers(self) -> Iterable[ResolvedComputeProvider]: ...

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]: ...

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider: ...


def internal_unit_identity(
    *,
    workspace_id: str,
    provider_ref: str,
    region: str,
    capability_key: str,
    root_volume_gib: int,
) -> tuple[str, UnitName]:
    """Derive the durable id and name of one provisioning unit.

    The seed is everything AWS pins to a single Auto Scaling group, root volume
    included: the volume size feeds the launch template, so two units differing
    only in it must not collide on one template.

    The derived name reaches the ASG and launch-template names, so changing this
    seed orphans the AWS resources of any unit created under the old one.
    """
    identity = uuid5(
        NAMESPACE_URL,
        "\0".join(
            (
                "compute-pool",
                workspace_id,
                provider_ref,
                region,
                capability_key,
                str(root_volume_gib),
            )
        ),
    )
    return str(identity), UnitName(f"managed-{identity.hex[:24]}")


def joined_unit_identity(
    *,
    workspace_id: str,
    pool: MachinePool,
    provider: str,
) -> tuple[str, UnitName]:
    """Derive the durable id and name of one joined-capacity unit.

    Machines reach this unit by joining rather than being bought, so the seed is
    only what decides which fleet a host lands in: the workspace, the pool it
    serves, and the kind of agent running it. Deriving rather than naming keeps
    find-or-create idempotent across restarts, and keeps the name out of the
    shape a pool label has — a unit called `lazycloud` sitting in pool
    `lazycloud` is the ambiguity this whole vocabulary exists to prevent.
    """
    identity = uuid5(
        NAMESPACE_URL,
        "\0".join(("compute-joined", workspace_id, pool, provider)),
    )
    return str(identity), UnitName(f"joined-{identity.hex[:24]}")


class ComputeSchedulerHooks(Protocol):
    def register_internal_unit(self, unit: ComputeUnitRecord, offer: ComputeOffer) -> None: ...

    def disable_machine(self, machine_id: str, reason: str) -> None: ...

    def machine_worker_availability(self, machine_id: str) -> MachineWorkerAvailability: ...

    def agent_intake_observing_since(self) -> datetime | None:
        """Since when some process has been receiving agent heartbeats, if any.

        The reclaim judges machines on silence, and silence means nothing while
        nothing was listening. `None` says no intake answers at all, which is a
        reason to decline the pass rather than a licence to run every clock.
        """
        ...

    def retire_machine(
        self,
        workspace_id: str,
        machine_id: str,
        reason: str,
    ) -> None: ...

    def revoke_unit_join_token(self, token_hash: str) -> None: ...
