from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
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
from shared.urls import normalize_http_origin

from compute.offers import ComputeOffer


class ProviderMachineStatus:
    Pending = "pending"
    Active = "active"
    Terminated = "terminated"
    Unhealthy = "unhealthy"
    Unknown = "unknown"


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
    worker_image_digest: str

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: str) -> str:
        return normalize_http_origin(value, field_name="control-plane URL")


class ProviderUnitRequest(ContractModel):
    workspace_id: str
    unit_id: str
    unit_name: UnitName
    provider_ref: str
    provider_connection_id: str
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
    """Serialize every provider mutation for one durable capacity owner."""

    def mutation_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class ResolvedComputeProvider:
    ref: str
    capacity_mode: ComputeCapacityMode
    connection_id: str | None = None
    direct: DirectMachineProvider | None = None
    pooled: PooledCapacityProvider | None = None

    def __post_init__(self) -> None:
        if self.capacity_mode is ComputeCapacityMode.Direct and (
            self.direct is None or self.pooled is not None
        ):
            raise ValueError("direct provider resolution is invalid")
        if self.capacity_mode is ComputeCapacityMode.Pooled and (
            self.pooled is None or self.direct is not None or self.connection_id is None
        ):
            raise ValueError("pooled provider resolution is invalid")


class ComputeProviderResolver(Protocol):
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

    def machine_worker_available(self, machine_id: str) -> bool: ...

    def retire_machine(
        self,
        workspace_id: str,
        machine_id: str,
        reason: str,
    ) -> None: ...

    def revoke_unit_join_token(self, token_hash: str) -> None: ...
