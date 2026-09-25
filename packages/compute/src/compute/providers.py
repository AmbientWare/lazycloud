from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator
from shared.capacity import CapacityFailureCode
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    UnitName,
)
from shared.contracts import ContractModel
from shared.errors import UpstreamUnavailableError
from shared.network_egress import NetworkEgressRouteEvidence
from shared.placement import Placement
from shared.timestamps import to_utc
from shared.urls import normalize_http_origin

from compute.block_volumes import BlockVolumeProvider, BlockVolumeProviders, BlockVolumeScope
from compute.offers import ComputeOffer
from compute.provider_nodes import ProviderNodeAdmission


class ProviderMachineStatus:
    Pending = "pending"
    Preparing = "preparing"
    Stopping = "stopping"
    Stopped = "stopped"
    Resuming = "resuming"
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
    stopped_machines: int = Field(default=0, ge=0)
    max_machines: int
    purchases_enabled: bool = True
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
        if self.desired_machines + self.stopped_machines > self.max_machines:
            raise ValueError("running and stopped commitments exceed the pool limit")
        if self.generation <= 0:
            raise ValueError("provider pool generation must be positive")
        return self


class ProviderUnitStateCheckpoints(Protocol):
    def load(self, request: ProviderUnitRequest) -> ComputeUnitProviderState: ...

    def save(
        self,
        request: ProviderUnitRequest,
        *,
        expected: ComputeUnitProviderState,
        state: ComputeUnitProviderState,
    ) -> None: ...


class ProviderUnitInstance(ContractModel):
    provider_instance_id: str
    status: str = ProviderMachineStatus.Unknown
    address: str = ""
    availability_zone: str = ""
    storage_volume_ids: tuple[str, ...] = ()
    # Opaque host configuration revision. Agent updates applied in place do not
    # change it. Empty means the provider has no configuration evidence.
    booted_template_version: str = ""
    hibernates: bool = False
    """Launched able to hibernate; the provider decides it per instance type at launch."""
    stop_requested: bool = False
    """The provider accepted a stop or hibernation for it that has not finished."""
    billing_started_at: datetime | None = None
    billing_minimum_seconds: int | None = Field(default=None, ge=0)
    billing_quantum_seconds: int | None = Field(default=None, ge=1)


class ProviderOfferEligibility(ContractModel):
    region: str = Field(min_length=1)
    instance_type: str = Field(min_length=1)
    preemptible: bool = False


class ProviderCapacityPolicy(ContractModel):
    model_config = ConfigDict(frozen=True)

    purchases_enabled: bool = True
    default_region: str
    allowed_regions: tuple[str, ...]
    root_volume_gib: int = Field(default=200, ge=50, le=2048)
    idle_timeout_seconds: int = Field(default=300, ge=60, le=86_400)
    allowed_offers: tuple[ProviderOfferEligibility, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> ProviderCapacityPolicy:
        if self.default_region not in self.allowed_regions:
            raise ValueError("provider default region must be allowed")
        identities = {
            (offer.region, offer.instance_type, offer.preemptible) for offer in self.allowed_offers
        }
        if len(identities) != len(self.allowed_offers):
            raise ValueError("approved offers must be unique per region, type and market")
        if any(offer.region not in self.allowed_regions for offer in self.allowed_offers):
            raise ValueError("approved offers must belong to allowed regions")
        return self

    def region_rank(self, region: str) -> int:
        """Where a region stands in `allowed_regions`, which lists them in preference order."""
        return (
            self.allowed_regions.index(region)
            if region in self.allowed_regions
            else len(self.allowed_regions)
        )

    def accepts(self, offer: ComputeOffer) -> bool:
        return offer.region in self.allowed_regions and any(
            limit.region == offer.region
            and limit.instance_type == offer.instance_type
            and limit.preemptible is offer.preemptible
            for limit in self.allowed_offers
        )


class ResolvedProviderPolicy(ProviderCapacityPolicy):
    workspace_id: str = Field(min_length=1)
    placement: Placement
    platform_fleet: bool

    @property
    def can_purchase(self) -> bool:
        return not self.platform_fleet or self.purchases_enabled


class ProviderDefinition(ContractModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    policy: ProviderCapacityPolicy
    workspace: str = "default"


class ProviderUnitSnapshot(ContractModel):
    phase: ProviderCapacityPhase
    resource_id: str = ""
    desired_machines: int = 0
    stopped_machines: int = 0
    max_machines: int = 0
    observed_machines: int = 0
    last_capacity_failure_at: datetime | None = None
    last_capacity_failure_code: CapacityFailureCode = CapacityFailureCode.ProviderLaunchFailed
    instances: list[ProviderUnitInstance] = Field(default_factory=list)
    provider_state: ComputeUnitProviderState = Field(default_factory=ComputeUnitProviderState)
    current_template_version: str = ""
    """Host revision new instances receive. Unknown revisions never authorize replacement."""


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
    def unbilled_network_destinations(
        self, unit: ComputeUnitRecord, provider_instance_id: str
    ) -> NetworkEgressRouteEvidence: ...

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]: ...

    def list_reserve_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
        """Offers whose instances can be prepared, stopped and resumed."""
        ...

    def complete_machine_preparation(
        self, request: ProviderUnitRequest, provider_instance_id: str, *, hibernate: bool
    ) -> ProviderUnitSnapshot:
        """Stop a prepared reserve, or start serving a resumed one.

        `hibernate` stops the reserve with its memory, which compute asks for only
        once the worker runs the release and waits at its first call.
        """
        ...

    def refresh_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        """Start one stopped reserve so its agent prepares it for the current release."""
        ...

    def stop_machine(
        self, request: ProviderUnitRequest, provider_instance_id: str
    ) -> ProviderUnitSnapshot:
        """Return one drained, cleaned instance to the stopped reserve."""
        ...

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        """Resolve owned capacity even when its shape is no longer sold."""
        ...

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot: ...

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot: ...

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        """Set desired capacity without choosing scale-in victims.

        Only release_machine may retire an exact live instance. Observed
        capacity may exceed desired capacity while the drain proceeds.
        """
        ...

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

    def block_volumes(self, region: str) -> BlockVolumeProvider:
        """Block volumes in this provider's account, for its machines in one region."""
        ...


class CapacityOwnerMutationLease(Protocol):
    """Serialize provider mutations and fence destructive changes from dispatch."""

    def mutation_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

    def dispatch_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

    def has_open_reservations(self, capacity_owner_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResolvedComputeProvider:
    ref: str
    capacity_mode: ComputeCapacityMode
    connection_id: str | None = None
    direct: DirectMachineProvider | None = None
    pooled: PooledCapacityProvider | None = None
    policy: ResolvedProviderPolicy | None = None
    node_admission: ProviderNodeAdmission | None = None

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


@dataclass(frozen=True, slots=True)
class ResolvedBlockVolumes(BlockVolumeProviders):
    """Block volumes from the pooled provider that owns a machine's capacity."""

    resolver: ComputeProviderResolver | None

    def volumes(self, scope: BlockVolumeScope) -> BlockVolumeProvider:
        if self.resolver is None:
            raise UpstreamUnavailableError(
                f"no compute provider is configured for {scope.provider_ref}, "
                "so its disk volumes cannot be managed"
            )
        resolved = self.resolver.resolve(scope.workspace_id, scope.provider_ref)
        if resolved.pooled is None:
            raise UpstreamUnavailableError(
                f"compute provider {scope.provider_ref} does not manage block volumes"
            )
        return resolved.pooled.block_volumes(scope.region)


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
    placement: Placement,
    provider: str,
) -> tuple[str, UnitName]:
    """Derive the durable id and name of one joined-capacity unit.

    Machines reach this unit by joining rather than being bought, so the seed is
    only what decides which fleet a host lands in: the workspace, the placement
    it serves, and the kind of agent running it. Deriving rather than naming keeps
    find-or-create idempotent across restarts.
    """
    identity = uuid5(
        NAMESPACE_URL,
        "\0".join(("compute-joined", workspace_id, placement.key, provider)),
    )
    return str(identity), UnitName(f"joined-{identity.hex[:24]}")


class MachineWorkerAvailability(StrEnum):
    """What the scheduler's hot state can say about one machine's worker.

    Three answers rather than two, because the reclaim terminates billable
    machines on this and a bool cannot separate "the worker says no" from "we
    have not heard". The hot record carries a short TTL, so its absence is a
    silence, not a verdict.
    """

    Available = "available"
    Unavailable = "unavailable"
    Unknown = "unknown"


class ComputeSchedulerHooks(Protocol):
    def register_internal_unit(self, unit: ComputeUnitRecord, offer: ComputeOffer) -> None: ...

    def disable_machine(self, machine_id: str, reason: str) -> None: ...

    def machine_worker_availability(self, machine_id: str) -> MachineWorkerAvailability: ...

    def machine_has_worker_update(self, capacity_owner_id: str, machine_id: str) -> bool: ...

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
