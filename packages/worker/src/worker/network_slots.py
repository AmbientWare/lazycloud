from __future__ import annotations

from enum import StrEnum

from foundation.validation import CidrValidation, validate_allow_list
from pydantic import Field, field_validator
from shared.contracts import ContractModel

from worker.execution import (
    DEFAULT_CONTAINER_SUBNET,
    container_ipv4_address_count,
    container_ipv6_address,
    container_veth_names,
    network_slot_reservation_id,
)
from worker.network_rules import container_network_comment

NETWORK_SLOT_POOL_LOCK_SUFFIX = "slot_pool"
NETWORK_SLOT_USABLE_RESERVED_ADDRESSES = 2


class NetworkSlotPoolFillReason(StrEnum):
    Closed = "closed"
    AlreadyRunning = "already-running"
    AtCapacity = "at-capacity"
    NeedsSlots = "needs-slots"


class NetworkSlotCleanupAction(StrEnum):
    Keep = "keep"
    Cleanup = "cleanup"
    SkipUnknownWorker = "skip-unknown-worker"


class NetworkSlotCleanupReason(StrEnum):
    LocalResourcesExist = "local-resources-exist"
    LocalResourcesMissing = "local-resources-missing"
    OtherWorkerAlive = "other-worker-alive"
    OtherWorkerDead = "other-worker-dead"
    OtherWorkerUnknown = "other-worker-unknown"


class NetworkRestrictionMode(StrEnum):
    Unrestricted = "unrestricted"
    Block = "block"
    Allowlist = "allowlist"


class NetworkSlot(ContractModel):
    slot_id: str
    reservation_id: str = ""
    namespace: str = ""
    veth_host: str = ""
    ip: str = ""
    ipv6: str = ""
    netns_path: str = ""

    @field_validator("slot_id")
    @classmethod
    def slot_id_must_not_be_empty(cls, value: str) -> str:
        if not value:
            msg = "network slot id cannot be empty"
            raise ValueError(msg)
        return value


class NetworkSlotPoolState(ContractModel):
    free_slot_ids: list[str] = Field(default_factory=list)
    total_slots: int = 0
    closed: bool = False
    fill_running: bool = False

    @field_validator("total_slots")
    @classmethod
    def total_slots_must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            msg = "network slot total cannot be negative"
            raise ValueError(msg)
        return value


class NetworkSlotPoolDrainPlan(ContractModel):
    drained_slot_ids: list[str]
    state: NetworkSlotPoolState


class NetworkSlotPoolFillPlan(ContractModel):
    should_fill: bool
    slots_to_create: int
    reason: NetworkSlotPoolFillReason


class NetworkSlotCleanupDecision(ContractModel):
    action: NetworkSlotCleanupAction
    reason: NetworkSlotCleanupReason
    current_worker_id: str
    slot_worker_id: str = ""
    resources_exist: bool = False

    @property
    def should_cleanup(self) -> bool:
        return self.action is NetworkSlotCleanupAction.Cleanup


class NetworkSlotDiscardPlan(ContractModel):
    slot: NetworkSlot
    container_id: str = ""
    reservation_id: str
    total_slots_before: int
    total_slots_after: int
    delete_veth_host: str
    delete_namespace: str
    clear_container_instance_ip: bool
    repository_container_ids_to_remove: list[str]
    ip_cache_keys_to_forget: list[str]


class ContainerNetworkInfo(ContractModel):
    container_id: str
    container_ip: str
    container_ipv6: str = ""
    namespace: str
    veth_host: str
    comment: str


class NetworkRestrictionPlan(ContractModel):
    mode: NetworkRestrictionMode
    apply: bool
    block_network: bool = False
    allow_list: list[str] = Field(default_factory=list)
    cidrs: list[CidrValidation] = Field(default_factory=list)

    @property
    def ipv4_allow_list(self) -> list[str]:
        return [entry.normalized for entry in self.cidrs if not entry.is_ipv6]

    @property
    def ipv6_allow_list(self) -> list[str]:
        return [entry.normalized for entry in self.cidrs if entry.is_ipv6]


def network_slot_pool_lock_prefix(network_prefix: str) -> str:
    return f"{network_prefix}:{NETWORK_SLOT_POOL_LOCK_SUFFIX}"


def is_redis_lock_not_obtained(error: BaseException | str | None) -> bool:
    return error is not None and "redislock: not obtained" in str(error)


def plan_network_slot_cleanup(
    *,
    current_worker_id: str,
    slot_worker_id: str = "",
    resources_exist: bool,
    slot_worker_alive: bool | None = None,
) -> NetworkSlotCleanupDecision:
    if slot_worker_id == "" or slot_worker_id == current_worker_id:
        if resources_exist:
            return NetworkSlotCleanupDecision(
                action=NetworkSlotCleanupAction.Keep,
                reason=NetworkSlotCleanupReason.LocalResourcesExist,
                current_worker_id=current_worker_id,
                slot_worker_id=slot_worker_id,
                resources_exist=resources_exist,
            )
        return NetworkSlotCleanupDecision(
            action=NetworkSlotCleanupAction.Cleanup,
            reason=NetworkSlotCleanupReason.LocalResourcesMissing,
            current_worker_id=current_worker_id,
            slot_worker_id=slot_worker_id,
            resources_exist=resources_exist,
        )
    if slot_worker_alive is None:
        return NetworkSlotCleanupDecision(
            action=NetworkSlotCleanupAction.SkipUnknownWorker,
            reason=NetworkSlotCleanupReason.OtherWorkerUnknown,
            current_worker_id=current_worker_id,
            slot_worker_id=slot_worker_id,
            resources_exist=resources_exist,
        )
    if slot_worker_alive:
        return NetworkSlotCleanupDecision(
            action=NetworkSlotCleanupAction.Keep,
            reason=NetworkSlotCleanupReason.OtherWorkerAlive,
            current_worker_id=current_worker_id,
            slot_worker_id=slot_worker_id,
            resources_exist=resources_exist,
        )
    return NetworkSlotCleanupDecision(
        action=NetworkSlotCleanupAction.Cleanup,
        reason=NetworkSlotCleanupReason.OtherWorkerDead,
        current_worker_id=current_worker_id,
        slot_worker_id=slot_worker_id,
        resources_exist=resources_exist,
    )


def plan_drain_free_network_slots(state: NetworkSlotPoolState) -> NetworkSlotPoolDrainPlan:
    drained = list(state.free_slot_ids)
    total_after = max(state.total_slots - len(drained), 0)
    return NetworkSlotPoolDrainPlan(
        drained_slot_ids=drained,
        state=state.model_copy(
            update={
                "free_slot_ids": [],
                "total_slots": total_after,
                "closed": True,
            }
        ),
    )


def plan_fill_network_slot_pool(
    state: NetworkSlotPoolState,
    *,
    pool_size: int,
) -> NetworkSlotPoolFillPlan:
    if state.closed:
        return NetworkSlotPoolFillPlan(
            should_fill=False,
            slots_to_create=0,
            reason=NetworkSlotPoolFillReason.Closed,
        )
    if state.fill_running:
        return NetworkSlotPoolFillPlan(
            should_fill=False,
            slots_to_create=0,
            reason=NetworkSlotPoolFillReason.AlreadyRunning,
        )
    needed = max(pool_size - state.total_slots, 0)
    if needed <= 0:
        return NetworkSlotPoolFillPlan(
            should_fill=False,
            slots_to_create=0,
            reason=NetworkSlotPoolFillReason.AtCapacity,
        )
    return NetworkSlotPoolFillPlan(
        should_fill=True,
        slots_to_create=needed,
        reason=NetworkSlotPoolFillReason.NeedsSlots,
    )


def plan_discard_network_slot(
    slot: NetworkSlot,
    *,
    container_id: str = "",
    total_slots: int,
    worker_id: str = "",
) -> NetworkSlotDiscardPlan:
    reservation_id = slot.reservation_id or network_slot_reservation_id(slot.slot_id, worker_id)
    repo_ids = [reservation_id]
    cache_keys = [reservation_id]
    if container_id:
        repo_ids.insert(0, container_id)
        cache_keys.insert(0, container_id)
    namespace = slot.namespace or slot.slot_id
    veth_host = slot.veth_host or container_veth_names(namespace)[0]
    return NetworkSlotDiscardPlan(
        slot=slot,
        container_id=container_id,
        reservation_id=reservation_id,
        total_slots_before=total_slots,
        total_slots_after=max(total_slots - 1, 0),
        delete_veth_host=veth_host,
        delete_namespace=namespace,
        clear_container_instance_ip=bool(container_id),
        repository_container_ids_to_remove=repo_ids,
        ip_cache_keys_to_forget=cache_keys,
    )


def container_network_info_from_slot(
    container_id: str,
    slot: NetworkSlot,
    *,
    ipv6_enabled: bool = False,
) -> ContainerNetworkInfo:
    namespace = slot.namespace or slot.slot_id
    veth_host = slot.veth_host or container_veth_names(namespace)[0]
    ipv6 = slot.ipv6
    if ipv6_enabled and not ipv6:
        ipv6 = container_ipv6_address(slot.ip)
    return ContainerNetworkInfo(
        container_id=container_id,
        container_ip=slot.ip,
        container_ipv6=ipv6 if ipv6_enabled else "",
        namespace=namespace,
        veth_host=veth_host,
        comment=container_network_comment(veth_host, container_id, namespace),
    )


def container_network_info_from_ip(
    container_id: str,
    container_ip: str,
    *,
    ipv6_enabled: bool = False,
) -> ContainerNetworkInfo:
    veth_host = container_veth_names(container_id)[0]
    return ContainerNetworkInfo(
        container_id=container_id,
        container_ip=container_ip,
        container_ipv6=container_ipv6_address(container_ip) if ipv6_enabled else "",
        namespace=container_id,
        veth_host=veth_host,
        comment=container_network_comment(veth_host, container_id, container_id),
    )


def plan_network_restriction(
    *,
    block_network: bool = False,
    allow_list: list[str] | None = None,
) -> NetworkRestrictionPlan:
    allow_list = allow_list or []
    if allow_list:
        cidrs = validate_allow_list(allow_list)
        return NetworkRestrictionPlan(
            mode=NetworkRestrictionMode.Allowlist,
            apply=True,
            block_network=block_network,
            allow_list=allow_list,
            cidrs=cidrs,
        )
    if block_network:
        return NetworkRestrictionPlan(
            mode=NetworkRestrictionMode.Block,
            apply=True,
            block_network=True,
        )
    return NetworkRestrictionPlan(mode=NetworkRestrictionMode.Unrestricted, apply=False)


def container_subnet_supports_usable_addresses(
    required: int,
    *,
    subnet: str = DEFAULT_CONTAINER_SUBNET,
) -> bool:
    if required <= 0:
        return True
    return (
        max(container_ipv4_address_count(subnet) - NETWORK_SLOT_USABLE_RESERVED_ADDRESSES, 0)
        >= required
    )
