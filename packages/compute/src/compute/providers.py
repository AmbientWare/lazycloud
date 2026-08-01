from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, JsonValue, SecretStr, field_validator, model_validator
from shared.app_identity import MACHINE_ID_LABEL
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputePoolProviderState,
    ComputePoolRecord,
)
from shared.contracts import ContractModel
from shared.urls import normalize_http_origin

from compute.offers import ComputeOffer
from compute.projection import PrivatePoolState


class ProviderMachineStatus:
    Pending = "pending"
    Active = "active"
    Terminated = "terminated"
    Unhealthy = "unhealthy"
    Unknown = "unknown"


class ProviderMachineReference(ContractModel):
    provider_instance_id: str
    machine_id: str
    name: str = ""
    status: str = ProviderMachineStatus.Unknown
    address: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    storage_volume_ids: tuple[str, ...] = ()


class ProviderReconcileResult(ContractModel):
    observed_machines: list[ProviderMachineReference] = Field(default_factory=list)
    missing_machine_ids: list[str] = Field(default_factory=list)
    stale_machine_ids: list[str] = Field(default_factory=list)
    terminated_machine_ids: list[str] = Field(default_factory=list)


class ProviderCapacityPhase(StrEnum):
    Provisioning = "provisioning"
    Ready = "ready"
    Degraded = "degraded"
    Deleting = "deleting"
    Deleted = "deleted"


class ProviderPoolBootstrap(ContractModel):
    control_plane_url: str
    enrollment_request_id: str
    agent_version: str
    agent_sha256: str
    agent_binary_url: str
    worker_image_digest: str
    # A node joins the tailnet from user-data and then reaches the control plane
    # as a peer, so this key is what makes `control_plane_url` a tailnet origin
    # rather than a public one. Blank is legal because resolving a pool for
    # deletion builds this contract without minting a key; building a launch
    # template without one is an error at the script assembler.
    tailnet_auth_key: SecretStr = SecretStr("")

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: str) -> str:
        return normalize_http_origin(value, field_name="control-plane URL")


class ProviderPoolRequest(ContractModel):
    workspace_id: str
    pool_id: str
    pool_name: str
    provider_ref: str
    provider_connection_id: str
    generation: int
    offer: ComputeOffer
    desired_machines: int
    max_machines: int
    root_volume_gib: int = 200
    bootstrap: ProviderPoolBootstrap
    provider_state: ComputePoolProviderState = Field(default_factory=ComputePoolProviderState)

    @model_validator(mode="after")
    def validate_pooled_capacity(self) -> ProviderPoolRequest:
        if self.offer.capacity_mode is not ComputeCapacityMode.Pooled:
            raise ValueError("pooled capacity requires a pooled provider offer")
        if self.desired_machines < 0:
            raise ValueError("desired machines cannot be negative")
        if self.max_machines <= 0 or self.desired_machines > self.max_machines:
            raise ValueError("invalid pooled capacity bounds")
        if self.generation <= 0:
            raise ValueError("provider pool generation must be positive")
        return self


class ProviderPoolInstance(ContractModel):
    provider_instance_id: str
    status: str = ProviderMachineStatus.Unknown
    address: str = ""
    availability_zone: str = ""
    storage_volume_ids: tuple[str, ...] = ()


class ProviderPoolSnapshot(ContractModel):
    phase: ProviderCapacityPhase
    resource_id: str = ""
    desired_machines: int = 0
    max_machines: int = 0
    observed_machines: int = 0
    instances: list[ProviderPoolInstance] = Field(default_factory=list)
    provider_state: ComputePoolProviderState = Field(default_factory=ComputePoolProviderState)


class DirectMachineLaunchRequest(ContractModel):
    workspace_id: str
    pool_name: str
    registration_token: str
    machine_id: str
    operation_id: str
    # Logical identity is `operation_id`; this rotates per launch attempt so a
    # compensated attempt relaunches instead of colliding with the provider's
    # idempotency record for the previous one.
    idempotency_key: str
    offer: ComputeOffer


class DirectMachineProvider(Protocol):
    def list_offers(self) -> Iterable[ComputeOffer]: ...

    def launch_machine(self, request: DirectMachineLaunchRequest) -> ProviderMachineReference: ...

    def reconcile_machines(
        self,
        pool_name: str,
        expected_machine_ids: set[str],
        *,
        terminate_stale: bool = False,
    ) -> ProviderReconcileResult: ...

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

    def ensure_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot: ...

    def describe_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot: ...

    def set_pool_capacity(
        self,
        request: ProviderPoolRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderPoolSnapshot: ...

    def release_machine(
        self,
        request: ProviderPoolRequest,
        provider_instance_id: str,
    ) -> ProviderPoolSnapshot: ...

    def delete_pool(self, request: ProviderPoolRequest) -> ProviderPoolSnapshot: ...

    def machine_storage_destroyed(
        self,
        request: ProviderPoolRequest,
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


def internal_pool_identity(
    *,
    workspace_id: str,
    provider_ref: str,
    region: str,
    capability_key: str,
) -> tuple[str, str]:
    identity = uuid5(
        NAMESPACE_URL,
        "\0".join(("compute-pool", workspace_id, provider_ref, region, capability_key)),
    )
    return str(identity), f"managed-{identity.hex[:24]}"


class MachineReferenceLike(Protocol):
    machine_id: str


class ComputeSchedulerHooks(Protocol):
    def register_pool(self, state: PrivatePoolState) -> None: ...

    def register_machine(self, machine: Machine) -> None: ...

    def register_internal_pool(self, pool: ComputePoolRecord, offer: ComputeOffer) -> None: ...

    def disable_machine(self, machine_id: str, reason: str) -> None: ...

    def machine_worker_available(self, machine_id: str) -> bool: ...

    def retire_machine(
        self,
        workspace_id: str,
        pool_name: str,
        machine_id: str,
        reason: str,
    ) -> None: ...

    def revoke_pool_join_token(self, token_hash: str) -> None: ...


def provider_machine_status[StatusT: StrEnum](
    raw: JsonValue,
    status_type: type[StatusT],
) -> StatusT:
    return status_type(_status_value(raw))


def provider_machine_id_from_item(item: Mapping[str, JsonValue]) -> str:
    raw_labels = item.get("labels")
    labels = raw_labels if isinstance(raw_labels, dict) else {}
    name = str(item.get("name", ""))
    return str(labels.get(MACHINE_ID_LABEL) or name.rsplit("-", 1)[-1])


def provider_machine_reference_fields[StatusT: StrEnum](
    item: Mapping[str, JsonValue],
    *,
    status_type: type[StatusT],
) -> dict[str, JsonValue]:
    return {
        "id": str(item.get("id", item.get("instance_id", ""))),
        "name": str(item.get("name", "")),
        "machine_id": provider_machine_id_from_item(item),
        "status": provider_machine_status(item.get("state", item.get("status")), status_type),
        "ip": str(item.get("public_ip", item.get("ip", "")) or ""),
    }


def provider_machine_reference_from_item[ReferenceT, StatusT: StrEnum](
    item: Mapping[str, JsonValue],
    *,
    reference_type: type[ReferenceT],
    status_type: type[StatusT],
) -> ReferenceT:
    return reference_type(**provider_machine_reference_fields(item, status_type=status_type))


def reconcile_provider_machines[MachineT: MachineReferenceLike](
    machines: Iterable[MachineT],
    expected_machine_ids: set[str],
    *,
    terminate_stale: bool,
    terminate: Callable[[MachineT], None],
) -> ProviderReconcileResult:
    machine_list = list(machines)
    by_machine_id = {str(machine.machine_id): machine for machine in machine_list}
    remote_ids = set(by_machine_id)
    stale = sorted(remote_ids - expected_machine_ids)
    terminated: list[str] = []
    if terminate_stale:
        for machine_id in stale:
            terminate(by_machine_id[machine_id])
            terminated.append(machine_id)
    return ProviderReconcileResult(
        missing_machine_ids=sorted(expected_machine_ids - remote_ids),
        stale_machine_ids=stale,
        terminated_machine_ids=terminated,
    )


def _status_value(raw: JsonValue) -> str:
    value = raw.value if isinstance(raw, StrEnum) else str(raw or "")
    normalized = value.lower()
    if normalized in {"running", "active", "ready", "available"}:
        return ProviderMachineStatus.Active
    if normalized in {"pending", "creating", "booting", "provisioning", "starting"}:
        return ProviderMachineStatus.Pending
    if normalized in {"terminated", "deleted", "deleting"}:
        return ProviderMachineStatus.Terminated
    if normalized in {"failed", "error", "unhealthy"}:
        return ProviderMachineStatus.Unhealthy
    return ProviderMachineStatus.Unknown


def _int(value: JsonValue) -> int:
    if value is None or value == "":
        return 0
    return int(float(str(value)))


def _float(value: JsonValue) -> float:
    if value is None or value == "":
        return 0.0
    return float(str(value))
