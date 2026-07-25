from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from fnmatch import fnmatch

from coordination.redis_client import RedisWireScalar
from redis.client import Pipeline, PubSub
from redis.typing import EncodableT, FieldT, KeyT, StreamIdT

type FakeRedisStreamEntry = tuple[str, dict[str, str]]
type FakeRedisStreamRead = list[str | list[FakeRedisStreamEntry]]


class FakeRedis:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._list_condition = threading.Condition(self._lock)
        self.blpop_started = threading.Event()
        self.values: dict[str, RedisWireScalar] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expirations: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.stream_ids: dict[str, int] = {}
        self.stream_entries: list[tuple[str, dict[str, str]]] = []
        self.xread_blocks: list[int | None] = []

    def set(
        self,
        name: str,
        value: RedisWireScalar,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        with self._lock:
            if nx and name in self.values:
                return False
            self.values[name] = value
            if ex is not None:
                self.expirations[name] = ex
            else:
                self.expirations.pop(name, None)
            return True

    def get(self, name: str) -> RedisWireScalar | None:
        return self.values.get(name)

    def getdel(self, name: str) -> RedisWireScalar | None:
        with self._lock:
            value = self.values.pop(name, None)
            self.expirations.pop(name, None)
            return value

    def incr(self, name: str) -> int:
        with self._lock:
            value = int(self.values.get(name, 0)) + 1
            self.values[name] = str(value)
            return value

    def decr(self, name: str) -> int:
        with self._lock:
            value = int(self.values.get(name, 0)) - 1
            self.values[name] = str(value)
            return value

    def strlen(self, name: str) -> int:
        value = self.values.get(name, b"")
        if not isinstance(value, (str, bytes)):
            return len(str(value))
        return len(value)

    def ttl(self, name: str) -> int:
        if not self._key_exists(name):
            return -2
        return self.expirations.get(name, -1)

    def exists(self, *names: str) -> int:
        return sum(int(self._key_exists(name)) for name in names)

    def delete(self, *names: str) -> int:
        with self._lock:
            removed = 0
            for name in names:
                existed = self._key_exists(name)
                self.values.pop(name, None)
                self.hashes.pop(name, None)
                self.lists.pop(name, None)
                self.sets.pop(name, None)
                self.zsets.pop(name, None)
                self.streams.pop(name, None)
                self.expirations.pop(name, None)
                removed += int(existed)
            return removed

    def sadd(self, name: str, *values: RedisWireScalar) -> int:
        members = self.sets.setdefault(name, set())
        before = len(members)
        members.update(str(value) for value in values)
        return len(members) - before

    def srem(self, name: str, *values: RedisWireScalar) -> int:
        members = self.sets.setdefault(name, set())
        before = len(members)
        members.difference_update(str(value) for value in values)
        return before - len(members)

    def smembers(self, name: str) -> set[str]:
        return set(self.sets.get(name, set()))

    def scard(self, name: str) -> int:
        return len(self.sets.get(name, set()))

    def sismember(self, name: str, value: str) -> bool:
        return value in self.sets.get(name, set())

    def expire(self, name: str, time: int) -> bool:
        self.expirations[name] = time
        return True

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> int:
        bucket = self.hashes.setdefault(name, {})
        before = len(bucket)
        if mapping is not None:
            bucket.update(mapping)
        elif key is not None and value is not None:
            bucket[key] = value
        else:
            msg = "field/value or mapping is required"
            raise ValueError(msg)
        return len(bucket) - before

    def hget(self, name: str, key: str) -> str | None:
        return self.hashes.get(name, {}).get(key)

    def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hashes.get(name, {}))

    def hdel(self, name: str, *keys: str) -> int:
        bucket = self.hashes.setdefault(name, {})
        before = len(bucket)
        for key in keys:
            bucket.pop(key, None)
        return before - len(bucket)

    def hlen(self, name: str) -> int:
        return len(self.hashes.get(name, {}))

    def rpush(self, name: str, *values: RedisWireScalar) -> int:
        with self._list_condition:
            bucket = self.lists.setdefault(name, [])
            bucket.extend(str(value) for value in values)
            self._list_condition.notify_all()
            return len(bucket)

    def lpop(self, name: str) -> str | None:
        with self._lock:
            values = self.lists.get(name, [])
            if not values:
                return None
            return values.pop(0)

    def blpop(self, keys: str | list[str], *, timeout: float) -> tuple[str, str] | None:
        name = keys[0] if isinstance(keys, list) else keys
        deadline = time.monotonic() + timeout
        with self._list_condition:
            self.blpop_started.set()
            while True:
                values = self.lists.get(name, [])
                if values:
                    return name, values.pop(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._list_condition.wait(remaining)

    def lrange(self, name: str, start: int, end: int) -> list[str]:
        values = self.lists.get(name, [])
        stop = None if end == -1 else end + 1
        return values[start:stop]

    def lindex(self, name: str, index: int) -> str | None:
        values = self.lists.get(name, [])
        try:
            return values[index]
        except IndexError:
            return None

    def llen(self, name: str) -> int:
        return len(self.lists.get(name, []))

    def zadd(self, name: str, mapping: Mapping[str, float]) -> int:
        bucket = self.zsets.setdefault(name, {})
        before = len(bucket)
        bucket.update({member: float(score) for member, score in mapping.items()})
        return len(bucket) - before

    def zrangebyscore(
        self,
        name: str,
        min: float | str,
        max: float | str,
    ) -> list[str]:
        minimum = _score_bound(min, negative=True)
        maximum = _score_bound(max, negative=False)
        values = [
            member
            for member, score in sorted(
                self.zsets.get(name, {}).items(),
                key=lambda item: (item[1], item[0]),
            )
            if minimum <= score <= maximum
        ]
        return values

    def zrange(self, name: str, start: int, end: int) -> list[str]:
        values = sorted(self.zsets.get(name, {}).items(), key=lambda item: (item[1], item[0]))
        stop = None if end == -1 else end + 1
        return [member for member, _score in values[start:stop]]

    def zscore(self, name: str, value: RedisWireScalar) -> float | None:
        return self.zsets.get(name, {}).get(str(value))

    def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    def zrem(self, key: str, *members: str) -> int:
        bucket = self.zsets.setdefault(key, {})
        removed = 0
        for member in members:
            removed += int(member in bucket)
            bucket.pop(member, None)
        return removed

    def zremrangebyscore(
        self,
        key: str,
        min_score: float | str,
        max_score: float | str,
    ) -> int:
        bucket = self.zsets.setdefault(key, {})
        minimum = _score_bound(min_score, negative=True)
        maximum = _score_bound(max_score, negative=False)
        remove = [member for member, score in bucket.items() if minimum <= score <= maximum]
        for member in remove:
            bucket.pop(member, None)
        return len(remove)

    def zrevrangebyscore(
        self,
        key: str,
        max_score: float | str,
        min_score: float | str,
        *,
        withscores: bool = False,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str] | list[tuple[str, float]]:
        minimum = _score_bound(min_score, negative=True)
        maximum = _score_bound(max_score, negative=False)
        rows = [
            (member, score)
            for member, score in self.zsets.get(key, {}).items()
            if minimum <= score <= maximum
        ]
        rows.sort(key=lambda item: (item[1], item[0]), reverse=True)
        if start is not None and num is not None:
            rows = rows[start : start + num]
        if withscores:
            return rows
        return [member for member, _score in rows]

    def publish(self, channel: str, message: RedisWireScalar) -> int:
        self.published.append((channel, str(message)))
        return 1

    def scan_iter(self, *, match: str, count: int) -> list[RedisWireScalar]:
        _ = count
        keys = (
            set(self.values)
            | set(self.hashes)
            | set(self.sets)
            | set(self.lists)
            | set(self.zsets)
            | set(self.streams)
        )
        return sorted(key for key in keys if fnmatch(key, match))

    def ping(self, **kwargs: RedisWireScalar) -> bool:
        _ = kwargs
        return True

    def close(self) -> None:
        return None

    def xadd(
        self,
        name: str,
        fields: dict[FieldT, EncodableT],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        _ = id, approximate
        next_id = self.stream_ids.get(name, 0) + 1
        self.stream_ids[name] = next_id
        entry_id = f"{next_id}-0"
        stored_fields = {str(key): str(value) for key, value in fields.items()}
        self.streams.setdefault(name, []).append((entry_id, stored_fields))
        if maxlen is not None:
            self.streams[name] = self.streams[name][-maxlen:]
        self.stream_entries.append((name, stored_fields))
        return entry_id

    def xrevrange(
        self,
        name: str,
        max: str = "+",
        min: str = "-",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        _ = max, min
        entries = list(reversed(self.streams.get(name, [])))
        if count is not None:
            entries = entries[:count]
        return [(entry_id, dict(fields)) for entry_id, fields in entries]

    def xread(
        self,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> list[FakeRedisStreamRead]:
        self.xread_blocks.append(block)
        response: list[FakeRedisStreamRead] = []
        for raw_stream, raw_last_id in streams.items():
            stream = (
                bytes(raw_stream).decode()
                if isinstance(raw_stream, memoryview)
                else str(raw_stream)
            )
            last_id = (
                bytes(raw_last_id).decode()
                if isinstance(raw_last_id, memoryview)
                else str(raw_last_id)
            )
            entries = [
                (entry_id, dict(fields))
                for entry_id, fields in self.streams.get(stream, [])
                if _stream_entry_number(entry_id) > _stream_entry_number(last_id)
            ]
            if count is not None:
                entries = entries[:count]
            if entries:
                response.append([stream, entries])
        return response

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> int:
        _ = script, numkeys, keys_and_args
        raise NotImplementedError("FakeRedis does not implement atomic Lua transitions")

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> PubSub:
        _ = ignore_subscribe_messages
        raise NotImplementedError("FakeRedis does not implement pub/sub")

    def pipeline(
        self,
        transaction: bool = True,
        shard_hint: str | None = None,
    ) -> Pipeline:
        _ = transaction, shard_hint
        raise NotImplementedError("FakeRedis does not implement Redis transactions")

    def _key_exists(self, key: str) -> bool:
        return (
            key in self.values
            or key in self.hashes
            or key in self.lists
            or key in self.sets
            or key in self.zsets
            or key in self.streams
        )


def _stream_entry_number(value: str) -> int:
    if value == "$":
        return 10**18
    try:
        return int(value.split("-", 1)[0])
    except ValueError:
        return 0


def _score_bound(value: float | str, *, negative: bool) -> float:
    if value == "-inf":
        return float("-inf")
    if value == "+inf":
        return float("inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf" if negative else "inf")
