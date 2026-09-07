from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from coordination.redis_client import RedisClient, RedisWireResponse, RedisWireScalar
from shared.errors import InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.collections import MapKeysResponse

from execution.collections.planning import (
    MapSetStatus,
    map_entry_key,
    map_index_key,
    map_registry_key,
    plan_map_delete,
    plan_map_live_keys,
    plan_map_set,
    plan_simple_queue_put,
    simple_queue_activity_key,
    simple_queue_name,
    simple_queue_registry_key,
    simple_queue_timestamps_name,
)

SIMPLE_QUEUE_ACTIVITY_RETENTION_SECONDS = 60 * 60

# SSCAN's COUNT is a hint. Preserve an offset within a returned batch so packed
# sets cannot turn one response into an unbounded key list or lose the remainder.
_MAP_KEYS_PAGE = """
local page = redis.call('SSCAN', KEYS[1], ARGV[1], 'MATCH', ARGV[4], 'COUNT', ARGV[3])
local offset = tonumber(ARGV[2])
local stop = math.min(#page[2], offset + tonumber(ARGV[3]))
local next_cursor = tostring(page[1]) .. ':0'
if stop < #page[2] then next_cursor = ARGV[1] .. ':' .. tostring(stop) end
if stop == #page[2] and tostring(page[1]) == '0' then next_cursor = '' end
local result = {next_cursor}
for i = offset + 1, stop do
  local entry_key = string.gsub(ARGV[5] .. page[2][i], ':+$', '')
  if redis.call('EXISTS', entry_key) == 1 then
    table.insert(result, page[2][i])
  end
end
return result
"""


@dataclass(frozen=True, slots=True)
class MapCollectionStats:
    name: str
    count: int
    size_bytes: int
    expiring_keys: int
    nearest_expiry_seconds: int | None


@dataclass(frozen=True, slots=True)
class SimpleQueueStats:
    name: str
    size: int
    oldest_message_age_seconds: float | None
    put_rate_per_minute: int


@dataclass(slots=True)
class RedisMapService:
    redis: RedisClient

    def map_keys_page(
        self,
        workspace_id: str,
        name: str,
        *,
        cursor: str = "",
        limit: int = 100,
        search: str = "",
    ) -> MapKeysResponse:
        if not 1 <= limit <= 100 or len(search) > 240:
            raise InvalidInputError(
                "Map key page limit must be 1 to 100 and search at most 240 characters"
            )
        if cursor and not re.fullmatch(r"[0-9]{1,20}:[0-9]{1,10}", cursor):
            raise InvalidInputError("Invalid map key cursor")
        scan_cursor, offset = (cursor or "0:0").split(":")
        pattern = "*" + re.sub(r"([\\*?\[\]])", r"\\\1", search) + "*"
        try:
            result = self.redis.eval_scalars(
                _MAP_KEYS_PAGE,
                1,
                self._key(map_index_key(workspace_id, name)),
                scan_cursor,
                offset,
                limit,
                pattern,
                self._key(map_index_key(workspace_id, name)).removesuffix("index"),
            )
        except Exception as exc:
            raise UpstreamUnavailableError("Map keys could not be read") from exc
        if not result:
            raise UpstreamUnavailableError("Redis returned an empty map key page")
        return MapKeysResponse(
            data=[_to_text(item) for item in result[1:]], next=_to_text(result[0])
        )

    def map_set(
        self,
        workspace_id: str,
        name: str,
        key: str,
        value: bytes,
        *,
        ttl_seconds: int = 0,
    ) -> None:
        plan = plan_map_set(workspace_id, name, key, value, ttl_seconds=ttl_seconds)
        if plan.status is not MapSetStatus.Accepted:
            raise InvalidInputError(plan.error_message)

        try:
            entry_key = self._key(plan.entry_key)
            index_key = self._key(plan.index_key)
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.set(entry_key, value, ex=ttl_seconds or None)
            pipeline.set_add(index_key, key)
            pipeline.set_add(self._key(map_registry_key(workspace_id)), name)
            pipeline.execute()
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def map_get(self, workspace_id: str, name: str, key: str) -> bytes:
        try:
            value = self.redis.get(self._key(map_entry_key(workspace_id, name, key)))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if value is None:
            raise NotFoundError(f"map key not found: {key}")
        return _to_bytes(value)

    def map_delete(self, workspace_id: str, name: str, key: str) -> None:
        plan = plan_map_delete(workspace_id, name, key)
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.delete(self._key(plan.entry_key))
            pipeline.set_remove(self._key(plan.index_key), key)
            pipeline.execute()
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def map_keys(self, workspace_id: str, name: str) -> tuple[str, ...]:
        index_key = self._key(map_index_key(workspace_id, name))
        try:
            indexed_keys = {_to_text(item) for item in self.redis.set_members(index_key)}
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not indexed_keys:
            return ()

        live_keys: set[str] = set()
        for key in indexed_keys:
            if self.redis.exists(self._key(map_entry_key(workspace_id, name, key))):
                live_keys.add(key)

        plan = plan_map_live_keys(
            workspace_id,
            name,
            indexed_keys=indexed_keys,
            existing_keys=live_keys,
        )
        if plan.stale_keys:
            self.redis.set_remove(index_key, *plan.stale_keys)
        return plan.live_keys

    def map_count(self, workspace_id: str, name: str) -> int:
        return len(self.map_keys(workspace_id, name))

    def map_stats(self, workspace_id: str, name: str) -> MapCollectionStats:
        keys = self.map_keys(workspace_id, name)
        if not keys:
            return MapCollectionStats(
                name=name,
                count=0,
                size_bytes=0,
                expiring_keys=0,
                nearest_expiry_seconds=None,
            )
        metrics = self._entry_metrics(workspace_id, name, keys)
        expiries = [ttl for _size, ttl in metrics if ttl >= 0]
        return MapCollectionStats(
            name=name,
            count=len(keys),
            size_bytes=sum(size for size, _ttl in metrics),
            expiring_keys=len(expiries),
            nearest_expiry_seconds=min(expiries) if expiries else None,
        )

    def map_names(self, workspace_id: str) -> tuple[str, ...]:
        registry_key = self._key(map_registry_key(workspace_id))
        try:
            names = sorted(_to_text(item) for item in self.redis.set_members(registry_key))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        live: list[str] = []
        stale: list[str] = []
        for name in names:
            (live if self.map_count(workspace_id, name) > 0 else stale).append(name)
        if stale:
            self.redis.set_remove(registry_key, *stale)
        return tuple(live)

    def delete_map(self, workspace_id: str, name: str) -> None:
        keys = self.map_keys(workspace_id, name)
        try:
            pipeline = self.redis.pipeline(transaction=True)
            for key in keys:
                pipeline.delete(self._key(map_entry_key(workspace_id, name, key)))
            pipeline.delete(self._key(map_index_key(workspace_id, name)))
            pipeline.set_remove(self._key(map_registry_key(workspace_id)), name)
            pipeline.execute()
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def delete_workspace(self, workspace_id: str) -> None:
        for name in self.map_names(workspace_id):
            self.delete_map(workspace_id, name)
        try:
            self.redis.delete(self._key(map_registry_key(workspace_id)))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def _key(self, logical_key: str) -> str:
        return self.redis.key(logical_key)

    def _entry_metrics(
        self,
        workspace_id: str,
        name: str,
        keys: tuple[str, ...],
    ) -> tuple[tuple[int, int], ...]:
        entry_keys = [self._key(map_entry_key(workspace_id, name, key)) for key in keys]
        try:
            pipeline = self.redis.pipeline(transaction=False)
            for entry_key in entry_keys:
                pipeline.string_length(entry_key)
                pipeline.ttl(entry_key)
            results = pipeline.execute()
            expected_results = len(entry_keys) * 2
            if len(results) != expected_results:
                msg = (
                    "Redis map metrics pipeline returned "
                    f"{len(results)} results; expected {expected_results}"
                )
                raise TypeError(msg)
            return tuple(
                (
                    _redis_integer(results[index], operation="strlen"),
                    _redis_integer(results[index + 1], operation="ttl"),
                )
                for index in range(0, len(results), 2)
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc


@dataclass(slots=True)
class RedisSimpleQueueService:
    redis: RedisClient
    clock: Callable[[], float] = time.time

    def simple_queue_put(
        self,
        workspace_id: str,
        name: str,
        value: bytes,
    ) -> None:
        plan = plan_simple_queue_put(workspace_id, name, value)
        recorded_at = self.clock()
        activity_key = self._key(simple_queue_activity_key(workspace_id, name))
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.list_push(self._key(plan.queue_key), value)
            pipeline.list_push(
                self._key(simple_queue_timestamps_name(workspace_id, name)),
                f"{recorded_at:.6f}",
            )
            pipeline.sorted_set_add(activity_key, {uuid.uuid4().hex: recorded_at})
            pipeline.sorted_set_remove_by_score(
                activity_key,
                "-inf",
                recorded_at - SIMPLE_QUEUE_ACTIVITY_RETENTION_SECONDS,
            )
            pipeline.expire(activity_key, SIMPLE_QUEUE_ACTIVITY_RETENTION_SECONDS)
            pipeline.set_add(self._key(simple_queue_registry_key(workspace_id)), name)
            pipeline.execute()
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def simple_queue_pop(self, workspace_id: str, name: str) -> bytes:
        queue_key = self._key(simple_queue_name(workspace_id, name))
        timestamps_key = self._key(simple_queue_timestamps_name(workspace_id, name))
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.list_pop(queue_key)
            pipeline.list_pop(timestamps_key)
            results = pipeline.execute()
            if len(results) != 2:
                msg = f"Redis queue pop pipeline returned {len(results)} results; expected 2"
                raise TypeError(msg)
            value = _optional_bytes(results[0])
            _optional_bytes(results[1])
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        return value or b""

    def simple_queue_peek(self, workspace_id: str, name: str) -> bytes:
        queue_key = self._key(simple_queue_name(workspace_id, name))
        try:
            values = self.redis.list_range(queue_key, 0, 0)
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not values:
            return b""
        return _to_bytes(values[0])

    def simple_queue_empty(self, workspace_id: str, name: str) -> bool:
        return self.simple_queue_size(workspace_id, name) == 0

    def simple_queue_size(self, workspace_id: str, name: str) -> int:
        try:
            return self.redis.list_length(self._key(simple_queue_name(workspace_id, name)))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def simple_queue_stats(
        self,
        workspace_id: str,
        name: str,
    ) -> SimpleQueueStats:
        recorded_at = self.clock()
        queue_key = self._key(simple_queue_name(workspace_id, name))
        timestamps_key = self._key(simple_queue_timestamps_name(workspace_id, name))
        activity_key = self._key(simple_queue_activity_key(workspace_id, name))
        try:
            size = self.redis.list_length(queue_key)
            timestamps = self.redis.list_range(timestamps_key, 0, 0)
            activity = self.redis.sorted_set_range_by_score(
                activity_key,
                recorded_at - 60,
                recorded_at,
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        oldest_age: float | None = None
        if timestamps:
            oldest_age = max(0.0, recorded_at - float(_to_text(timestamps[0])))
        return SimpleQueueStats(
            name=name,
            size=size,
            oldest_message_age_seconds=oldest_age,
            put_rate_per_minute=len(activity),
        )

    def simple_queue_names(self, workspace_id: str) -> tuple[str, ...]:
        registry_key = self._key(simple_queue_registry_key(workspace_id))
        try:
            names = sorted(_to_text(item) for item in self.redis.set_members(registry_key))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        return tuple(names)

    def delete_queue(self, workspace_id: str, name: str) -> None:
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.delete(self._key(simple_queue_name(workspace_id, name)))
            pipeline.delete(self._key(simple_queue_timestamps_name(workspace_id, name)))
            pipeline.delete(self._key(simple_queue_activity_key(workspace_id, name)))
            pipeline.set_remove(self._key(simple_queue_registry_key(workspace_id)), name)
            pipeline.execute()
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def delete_workspace(self, workspace_id: str) -> None:
        for name in self.simple_queue_names(workspace_id):
            self.delete_queue(workspace_id, name)
        try:
            self.redis.delete(self._key(simple_queue_registry_key(workspace_id)))
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def _key(self, logical_key: str) -> str:
        return self.redis.key(logical_key)


def _optional_bytes(value: RedisWireResponse) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes, int, float, bool)):
        raise TypeError("Redis collection value must be a wire scalar")
    return _to_bytes(value)


def _to_bytes(value: RedisWireScalar) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    msg = f"Redis value must be bytes or text, got {type(value).__name__}"
    raise TypeError(msg)


def _to_text(value: RedisWireScalar) -> str:
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, str):
        return value
    msg = f"Redis value must be bytes or text, got {type(value).__name__}"
    raise TypeError(msg)


def _redis_integer(value: RedisWireResponse, *, operation: str) -> int:
    if type(value) is int:
        return value
    msg = f"Redis {operation} result must be an integer"
    raise TypeError(msg)
