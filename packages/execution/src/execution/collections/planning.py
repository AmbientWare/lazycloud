from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel
from shared.http.collections import MAX_MAP_TTL_SECONDS

MAX_MAP_VALUE_SIZE_BYTES = (1024 * 1024) + 13

MAP_KEY_PREFIX = "map"
SIMPLE_QUEUE_KEY_PREFIX = "simplequeue"


class MapOperation(StrEnum):
    Set = "set"
    Delete = "delete"
    Keys = "keys"


class MapSetStatus(StrEnum):
    Accepted = "accepted"
    ValueTooLarge = "value-too-large"
    TtlTooLong = "ttl-too-long"


class MapSetPlan(ContractModel):
    operation: MapOperation = MapOperation.Set
    status: MapSetStatus
    entry_key: str
    index_key: str
    value_size_bytes: int
    ttl_seconds: int
    error_message: str = ""


class MapDeletePlan(ContractModel):
    operation: MapOperation = MapOperation.Delete
    entry_key: str
    index_key: str


class MapLiveKeysPlan(ContractModel):
    operation: MapOperation = MapOperation.Keys
    index_key: str
    live_keys: tuple[str, ...]
    stale_keys: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.live_keys)


class SimpleQueuePutPlan(ContractModel):
    queue_key: str
    value_size_bytes: int = Field(ge=0)


def map_entry_key(workspace_id: str, name: str, key: str) -> str:
    return f"{MAP_KEY_PREFIX}:{workspace_id}:{name}:{key}"


def map_index_key(workspace_id: str, name: str) -> str:
    return f"{MAP_KEY_PREFIX}:{workspace_id}:{name}:index"


def map_registry_key(workspace_id: str) -> str:
    return f"{MAP_KEY_PREFIX}-registry:{workspace_id}"


def plan_map_set(
    workspace_id: str,
    name: str,
    key: str,
    value: bytes,
    *,
    ttl_seconds: int = 0,
) -> MapSetPlan:
    entry_key = map_entry_key(workspace_id, name, key)
    index_key = map_index_key(workspace_id, name)
    value_size = len(value)

    if value_size > MAX_MAP_VALUE_SIZE_BYTES:
        return MapSetPlan(
            status=MapSetStatus.ValueTooLarge,
            entry_key=entry_key,
            index_key=index_key,
            value_size_bytes=value_size,
            ttl_seconds=ttl_seconds,
            error_message="Value cannot be larger than 1 MiB",
        )

    if ttl_seconds > MAX_MAP_TTL_SECONDS:
        return MapSetPlan(
            status=MapSetStatus.TtlTooLong,
            entry_key=entry_key,
            index_key=index_key,
            value_size_bytes=value_size,
            ttl_seconds=ttl_seconds,
            error_message="TTL cannot be longer than 1 week",
        )

    return MapSetPlan(
        status=MapSetStatus.Accepted,
        entry_key=entry_key,
        index_key=index_key,
        value_size_bytes=value_size,
        ttl_seconds=ttl_seconds,
    )


def plan_map_delete(workspace_id: str, name: str, key: str) -> MapDeletePlan:
    return MapDeletePlan(
        entry_key=map_entry_key(workspace_id, name, key),
        index_key=map_index_key(workspace_id, name),
    )


def plan_map_live_keys(
    workspace_id: str,
    name: str,
    *,
    indexed_keys: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    existing_keys: set[str] | frozenset[str] | list[str] | tuple[str, ...],
) -> MapLiveKeysPlan:
    indexed = set(indexed_keys)
    existing = set(existing_keys)
    live = tuple(sorted(indexed & existing))
    stale = tuple(sorted(indexed - existing))
    return MapLiveKeysPlan(
        index_key=map_index_key(workspace_id, name),
        live_keys=live,
        stale_keys=stale,
    )


def simple_queue_name(workspace_id: str, name: str) -> str:
    return f"{SIMPLE_QUEUE_KEY_PREFIX}:{workspace_id}:{name}"


def simple_queue_timestamps_name(workspace_id: str, name: str) -> str:
    return f"{simple_queue_name(workspace_id, name)}:timestamps"


def simple_queue_activity_key(workspace_id: str, name: str) -> str:
    return f"{simple_queue_name(workspace_id, name)}:activity"


def simple_queue_registry_key(workspace_id: str) -> str:
    return f"{SIMPLE_QUEUE_KEY_PREFIX}-registry:{workspace_id}"


def plan_simple_queue_put(workspace_id: str, name: str, value: bytes) -> SimpleQueuePutPlan:
    return SimpleQueuePutPlan(
        queue_key=simple_queue_name(workspace_id, name),
        value_size_bytes=len(value),
    )
