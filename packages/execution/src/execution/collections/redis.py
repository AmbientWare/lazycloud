from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from coordination.redis_client import RedisClient, RedisWireResponse, RedisWireScalar
from shared.errors import ConflictError, InvalidInputError, NotFoundError, UpstreamUnavailableError

from execution.collections import map_scripts
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


@dataclass(frozen=True, slots=True)
class MapEntry:
    value: bytes
    revision: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class MapKeyPage:
    data: tuple[str, ...]
    next: str | None


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

    def map_set(
        self,
        workspace_id: str,
        name: str,
        key: str,
        value: bytes,
        *,
        ttl_seconds: int | None = 0,
        if_revision: str | None = None,
        if_absent: bool = False,
    ) -> None:
        plan = plan_map_set(workspace_id, name, key, value, ttl_seconds=ttl_seconds or 0)
        if plan.status is not MapSetStatus.Accepted:
            raise InvalidInputError(plan.error_message)
        if plan.entry_key.rstrip(":") == plan.index_key:
            raise InvalidInputError("The map key 'index' is reserved")
        if if_absent and if_revision is not None:
            raise InvalidInputError("Choose if_absent or if_revision, not both")

        try:
            written = self.redis.eval_int(
                map_scripts.WRITE,
                3,
                self._key(plan.entry_key),
                self._key(plan.index_key),
                self._key(map_registry_key(workspace_id)),
                key,
                name,
                value,
                "keep" if ttl_seconds is None else str(ttl_seconds),
                "absent" if if_absent else "revision" if if_revision is not None else "any",
                if_revision or "",
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not written:
            raise ConflictError("Map key changed or expired. Reload it before saving.")

    def map_get(self, workspace_id: str, name: str, key: str) -> MapEntry:
        try:
            result = self.redis.eval_scalars(
                map_scripts.READ, 1, self._key(map_entry_key(workspace_id, name, key))
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not result:
            raise NotFoundError(f"map key not found: {key}")
        expiry = _redis_integer(result[2], operation="PEXPIRETIME")
        return MapEntry(
            value=_to_bytes(result[0]),
            revision=_to_text(result[1]),
            expires_at=datetime.fromtimestamp(expiry / 1000, UTC) if expiry >= 0 else None,
        )

    def map_delete(
        self, workspace_id: str, name: str, key: str, *, if_revision: str | None = None
    ) -> None:
        plan = plan_map_delete(workspace_id, name, key)
        if plan.entry_key.rstrip(":") == plan.index_key:
            raise InvalidInputError("The map key 'index' is reserved")
        try:
            deleted = self.redis.eval_int(
                map_scripts.DELETE,
                2,
                self._key(plan.entry_key),
                self._key(plan.index_key),
                key,
                if_revision or "",
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not deleted:
            raise ConflictError("Map key changed or expired. Reload it before deleting.")

    def map_key_page(
        self,
        workspace_id: str,
        name: str,
        *,
        cursor: str | None = None,
        prefix: str = "",
        limit: int = 100,
    ) -> MapKeyPage:
        if not 1 <= limit <= 500 or (cursor is not None and not cursor.isdecimal()):
            raise InvalidInputError("Invalid map key cursor or page size")
        try:
            result = self.redis.eval_scalars(
                map_scripts.KEY_PAGE,
                1,
                self._key(map_index_key(workspace_id, name)),
                cursor or "0",
                limit,
                self._entry_prefix(workspace_id, name),
                prefix,
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        next_cursor = _to_text(result[0])
        return MapKeyPage(
            data=tuple(_to_text(value) for value in result[1:]),
            next=None if next_cursor == "0" else next_cursor,
        )

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
        try:
            self.redis.eval_int(
                map_scripts.DELETE_MAP,
                2,
                self._key(map_index_key(workspace_id, name)),
                self._key(map_registry_key(workspace_id)),
                self._entry_prefix(workspace_id, name),
                name,
            )
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    def _entry_prefix(self, workspace_id: str, name: str) -> str:
        # Scripts append the logical key before applying RedisClient.key's trailing-colon trim.
        return f"{self.redis.key_prefix}:{map_entry_key(workspace_id, name, '')}"

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
