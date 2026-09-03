from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Protocol, runtime_checkable

from coordination.redis_client import RedisWireResponse, RedisWireScalar
from lupa.lua51 import LuaRuntime, lua_type
from pydantic import JsonValue
from redis.client import Pipeline, PubSub
from redis.exceptions import DataError, ResponseError
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
        self.pexpirations: dict[str, int] = {}
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
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        with self._lock:
            if nx and name in self.values:
                return False
            self.values[name] = value
            if px is not None:
                self.pexpirations[name] = px
                self.expirations.pop(name, None)
            elif ex is not None:
                self.expirations[name] = ex
                self.pexpirations.pop(name, None)
            else:
                self.expirations.pop(name, None)
                self.pexpirations.pop(name, None)
            return True

    def get(self, name: str) -> RedisWireScalar | None:
        return self.values.get(name)

    def mget(self, keys: Iterable[str]) -> list[RedisWireScalar | None]:
        return [self.values.get(key) for key in keys]

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
        mapping: Mapping[FieldT, EncodableT] | None = None,
    ) -> int:
        bucket = self.hashes.setdefault(name, {})
        before = len(bucket)
        if mapping is not None:
            bucket.update({str(field): str(item) for field, item in mapping.items()})
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

    def hexists(self, name: str, key: str) -> bool:
        return key in self.hashes.get(name, {})

    def hincrby(self, name: str, key: str, amount: int) -> int:
        with self._lock:
            bucket = self.hashes.setdefault(name, {})
            value = int(bucket.get(key, "0")) + amount
            bucket[key] = str(value)
            return value

    def persist(self, name: str) -> bool:
        with self._lock:
            if not self._key_exists(name):
                return False
            return self.expirations.pop(name, None) is not None

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

    def blmove(
        self,
        first_list: str,
        second_list: str,
        timeout: int,
    ) -> str | None:
        deadline = time.monotonic() + timeout
        with self._list_condition:
            self.blpop_started.set()
            while True:
                values = self.lists.get(first_list, [])
                if values:
                    moved = values.pop(0)
                    self.lists.setdefault(second_list, []).append(moved)
                    return moved
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

    def lrem(self, name: str, count: int, value: str) -> int:
        with self._lock:
            values = self.lists.get(name)
            if values is None:
                return 0
            if count == 0:
                removed = values.count(value)
                values[:] = [item for item in values if item != value]
                return removed
            positions = range(len(values)) if count > 0 else range(len(values) - 1, -1, -1)
            doomed: set[int] = set()
            for position in positions:
                if values[position] != value:
                    continue
                doomed.add(position)
                if len(doomed) == abs(count):
                    break
            values[:] = [item for index, item in enumerate(values) if index not in doomed]
            return len(doomed)

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
        name: str,
        min: float | str,
        max: float | str,
    ) -> int:
        bucket = self.zsets.setdefault(name, {})
        minimum = _score_bound(min, negative=True)
        maximum = _score_bound(max, negative=False)
        remove = [member for member, score in bucket.items() if minimum <= score <= maximum]
        for member in remove:
            bucket.pop(member, None)
        return len(remove)

    def publish(self, channel: str, message: RedisWireScalar) -> int:
        self.published.append((channel, str(message)))
        return 1

    def scan(
        self,
        cursor: int,
        *,
        match: str,
        count: int,
    ) -> tuple[int, list[RedisWireScalar]]:
        _ = cursor, count
        keys = (
            set(self.values)
            | set(self.hashes)
            | set(self.sets)
            | set(self.lists)
            | set(self.zsets)
            | set(self.streams)
        )
        return 0, sorted(key for key in keys if fnmatch(key, match))

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

    def xrange(self, name: str, count: int | None = None) -> list[tuple[str, dict[str, str]]]:
        entries = list(self.streams.get(name, []))
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
    ) -> RedisWireResponse:
        """Run the caller's Lua body against this store, atomically, as Redis does."""

        if numkeys < 0 or numkeys > len(keys_and_args):
            raise ResponseError("Number of keys can't be greater than number of args")
        arguments = [_wire_text(value) for value in keys_and_args]
        with self._lock:
            return _LuaScriptRun(self).execute(script, arguments[:numkeys], arguments[numkeys:])

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


@dataclass(frozen=True, slots=True)
class _StatusReply:
    """A Redis status reply, which Lua receives as a table carrying an ``ok`` field."""

    text: str


type _CommandReply = str | int | _StatusReply | list[_CommandReply] | None


@runtime_checkable
class _LuaTable(Protocol):
    """The Lua table surface reply conversion reads back out of the interpreter."""

    def __getitem__(self, key: str | int, /) -> object: ...


class _LuaScriptRun:
    """Execute one production Lua body over a `FakeRedis` store.

    The scripts are never reimplemented here: the caller's own Lua source runs on
    a Lua 5.1 interpreter, the version Redis embeds, so control flow, `unpack`,
    `tonumber`, `cjson`, and every return value come from production's own text.
    `redis.call` is the only bridge, and it dispatches into the same `FakeRedis`
    storage the direct commands mutate, so a script and a direct command can never
    observe two different stores. Every command, option, argument type, and reply
    type the bridge cannot honour exactly raises instead of guessing.
    """

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._runtime = LuaRuntime(unpack_returned_tuples=False)
        self._commands: dict[str, Callable[[Sequence[str]], _CommandReply]] = {
            "DEL": self._delete,
            "DECR": self._decrement,
            "EXISTS": self._exists,
            "EXPIRE": self._expire,
            "GET": self._get,
            "HDEL": self._hash_delete,
            "HEXISTS": self._hash_exists,
            "HGET": self._hash_get,
            "HINCRBY": self._hash_increment,
            "HSET": self._hash_set,
            "INCR": self._increment,
            "LLEN": self._list_length,
            "LPOP": self._list_pop,
            "LRANGE": self._list_range,
            "LREM": self._list_remove,
            "PERSIST": self._persist,
            "RPUSH": self._list_push,
            "SADD": self._set_add,
            "SET": self._set,
            "SREM": self._set_remove,
            "XADD": self._stream_add,
            "XRANGE": self._stream_range,
            "ZADD": self._sorted_set_add,
            "ZRANGEBYSCORE": self._sorted_set_range_by_score,
            "ZREM": self._sorted_set_remove,
        }

    def execute(
        self,
        script: str,
        keys: Sequence[str],
        argv: Sequence[str],
    ) -> RedisWireResponse:
        environment = self._runtime.globals()
        # Redis exposes no host bridge to a script; drop the one lupa installs.
        environment["python"] = None
        environment["KEYS"] = self._runtime.table_from(list(keys))
        environment["ARGV"] = self._runtime.table_from(list(argv))
        environment["redis"] = self._runtime.table_from({"call": self._call})
        environment["cjson"] = self._runtime.table_from({"decode": self._decode_json})
        return self._from_lua(self._runtime.execute(script))

    def _call(self, *arguments: object) -> object:
        if not arguments:
            raise ResponseError("Please specify at least one argument for this redis lib call")
        name = _lua_text(arguments[0]).upper()
        command = self._commands.get(name)
        if command is None:
            raise ResponseError(f"Unknown Redis command '{name}' called from script")
        return self._to_lua(command([_lua_text(value) for value in arguments[1:]]))

    def _decode_json(self, document: object) -> object:
        decoded: JsonValue = json.loads(_lua_text(document))
        return self._to_lua_json(decoded)

    def _to_lua(self, reply: _CommandReply) -> object:
        if reply is None:
            return False
        if isinstance(reply, _StatusReply):
            return self._runtime.table_from({"ok": reply.text})
        if isinstance(reply, list):
            return self._runtime.table_from([self._to_lua(item) for item in reply])
        return reply

    def _to_lua_json(self, document: JsonValue) -> object:
        if isinstance(document, dict):
            return self._runtime.table_from(
                {key: self._to_lua_json(value) for key, value in document.items()}
            )
        if isinstance(document, list):
            return self._runtime.table_from([self._to_lua_json(item) for item in document])
        return document

    def _from_lua(self, value: object) -> RedisWireResponse:
        if value is None or value is False:
            return None
        if value is True:
            return 1
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            return value
        if lua_type(value) != "table" or not isinstance(value, _LuaTable):
            raise ResponseError("Lua script returned a value Redis cannot convert to a reply")
        error = value["err"]
        if error is not None:
            raise ResponseError(_lua_text(error))
        status = value["ok"]
        if status is not None:
            return _lua_text(status)
        reply: list[RedisWireResponse] = []
        position = 1
        while True:
            item = value[position]
            if item is None:
                return reply
            reply.append(self._from_lua(item))
            position += 1

    def _get(self, arguments: Sequence[str]) -> _CommandReply:
        (key,) = _fixed(arguments, 1, "GET")
        return _stored_text(self._redis.get(key))

    def _set(self, arguments: Sequence[str]) -> _CommandReply:
        if len(arguments) < 2:
            raise ResponseError("wrong number of arguments for 'set' command")
        key, value = arguments[0], arguments[1]
        expire_seconds: int | None = None
        only_if_absent = False
        options = list(arguments[2:])
        while options:
            option = options.pop(0).upper()
            if option == "EX":
                expire_seconds = int(options.pop(0))
            elif option == "NX":
                only_if_absent = True
            else:
                raise ResponseError(f"FakeRedis does not implement SET option '{option}'")
        stored = self._redis.set(key, value, ex=expire_seconds, nx=only_if_absent)
        return _StatusReply("OK") if stored else None

    def _delete(self, arguments: Sequence[str]) -> _CommandReply:
        return self._redis.delete(*_at_least(arguments, 1, "DEL"))

    def _exists(self, arguments: Sequence[str]) -> _CommandReply:
        return self._redis.exists(*_at_least(arguments, 1, "EXISTS"))

    def _expire(self, arguments: Sequence[str]) -> _CommandReply:
        key, seconds = _fixed(arguments, 2, "EXPIRE")
        if not self._redis.exists(key):
            return 0
        self._redis.expire(key, int(seconds))
        return 1

    def _persist(self, arguments: Sequence[str]) -> _CommandReply:
        (key,) = _fixed(arguments, 1, "PERSIST")
        return int(self._redis.persist(key))

    def _increment(self, arguments: Sequence[str]) -> _CommandReply:
        (key,) = _fixed(arguments, 1, "INCR")
        return self._redis.incr(key)

    def _decrement(self, arguments: Sequence[str]) -> _CommandReply:
        (key,) = _fixed(arguments, 1, "DECR")
        return self._redis.decr(key)

    def _hash_get(self, arguments: Sequence[str]) -> _CommandReply:
        key, field = _fixed(arguments, 2, "HGET")
        return self._redis.hget(key, field)

    def _hash_set(self, arguments: Sequence[str]) -> _CommandReply:
        if len(arguments) < 3 or len(arguments) % 2 != 1:
            raise ResponseError("wrong number of arguments for 'hset' command")
        key = arguments[0]
        fields: dict[FieldT, EncodableT] = dict(zip(arguments[1::2], arguments[2::2], strict=True))
        return self._redis.hset(key, mapping=fields)

    def _hash_delete(self, arguments: Sequence[str]) -> _CommandReply:
        key, *fields = _at_least(arguments, 2, "HDEL")
        return self._redis.hdel(key, *fields)

    def _hash_exists(self, arguments: Sequence[str]) -> _CommandReply:
        key, field = _fixed(arguments, 2, "HEXISTS")
        return int(self._redis.hexists(key, field))

    def _hash_increment(self, arguments: Sequence[str]) -> _CommandReply:
        key, field, amount = _fixed(arguments, 3, "HINCRBY")
        return self._redis.hincrby(key, field, int(amount))

    def _set_add(self, arguments: Sequence[str]) -> _CommandReply:
        key, *members = _at_least(arguments, 2, "SADD")
        return self._redis.sadd(key, *members)

    def _set_remove(self, arguments: Sequence[str]) -> _CommandReply:
        key, *members = _at_least(arguments, 2, "SREM")
        return self._redis.srem(key, *members)

    def _list_push(self, arguments: Sequence[str]) -> _CommandReply:
        key, *values = _at_least(arguments, 2, "RPUSH")
        return self._redis.rpush(key, *values)

    def _list_pop(self, arguments: Sequence[str]) -> _CommandReply:
        (key,) = _fixed(arguments, 1, "LPOP")
        return self._redis.lpop(key)

    def _list_length(self, arguments: Sequence[str]) -> _CommandReply:
        (key,) = _fixed(arguments, 1, "LLEN")
        return self._redis.llen(key)

    def _list_range(self, arguments: Sequence[str]) -> _CommandReply:
        key, start, stop = _fixed(arguments, 3, "LRANGE")
        entries: list[_CommandReply] = list(self._redis.lrange(key, int(start), int(stop)))
        return entries

    def _list_remove(self, arguments: Sequence[str]) -> _CommandReply:
        key, count, value = _fixed(arguments, 3, "LREM")
        return self._redis.lrem(key, int(count), value)

    def _sorted_set_add(self, arguments: Sequence[str]) -> _CommandReply:
        if len(arguments) < 3 or len(arguments) % 2 != 1:
            raise ResponseError("wrong number of arguments for 'zadd' command")
        key = arguments[0]
        scored = {
            member: float(score)
            for score, member in zip(arguments[1::2], arguments[2::2], strict=True)
        }
        return self._redis.zadd(key, scored)

    def _sorted_set_remove(self, arguments: Sequence[str]) -> _CommandReply:
        key, *members = _at_least(arguments, 2, "ZREM")
        return self._redis.zrem(key, *members)

    def _sorted_set_range_by_score(self, arguments: Sequence[str]) -> _CommandReply:
        if len(arguments) not in (3, 6):
            raise ResponseError("FakeRedis implements ZRANGEBYSCORE with an optional LIMIT only")
        key, minimum, maximum = arguments[0], arguments[1], arguments[2]
        members: list[_CommandReply] = list(self._redis.zrangebyscore(key, minimum, maximum))
        if len(arguments) == 3:
            return members
        if arguments[3].upper() != "LIMIT":
            raise ResponseError(f"FakeRedis does not implement ZRANGEBYSCORE '{arguments[3]}'")
        offset, count = int(arguments[4]), int(arguments[5])
        return members[offset:] if count < 0 else members[offset : offset + count]

    def _stream_add(self, arguments: Sequence[str]) -> _CommandReply:
        if len(arguments) < 4:
            raise ResponseError("wrong number of arguments for 'xadd' command")
        key = arguments[0]
        position = 1
        maxlen: int | None = None
        approximate = False
        if arguments[position].upper() == "MAXLEN":
            position += 1
            if arguments[position] in ("~", "="):
                approximate = arguments[position] == "~"
                position += 1
            maxlen = int(arguments[position])
            position += 1
        entry_id = arguments[position]
        if entry_id != "*":
            raise ResponseError("FakeRedis assigns stream entry identifiers and requires '*'")
        position += 1
        remaining = arguments[position:]
        if not remaining or len(remaining) % 2 != 0:
            raise ResponseError("wrong number of arguments for 'xadd' command")
        fields: dict[FieldT, EncodableT] = dict(zip(remaining[0::2], remaining[1::2], strict=True))
        return self._redis.xadd(key, fields, maxlen=maxlen, approximate=approximate)

    def _stream_range(self, arguments: Sequence[str]) -> _CommandReply:
        if len(arguments) not in (3, 5):
            raise ResponseError("FakeRedis implements XRANGE with an optional COUNT only")
        key, minimum, maximum = arguments[0], arguments[1], arguments[2]
        if minimum != "-" or maximum != "+":
            raise ResponseError("FakeRedis implements XRANGE over the full stream only")
        count: int | None = None
        if len(arguments) == 5:
            if arguments[3].upper() != "COUNT":
                raise ResponseError(f"FakeRedis does not implement XRANGE '{arguments[3]}'")
            count = int(arguments[4])
        entries: list[_CommandReply] = []
        for entry_id, entry_fields in self._redis.xrange(key, count=count):
            flattened: list[_CommandReply] = []
            for field, value in entry_fields.items():
                flattened.append(field)
                flattened.append(value)
            entries.append([entry_id, flattened])
        return entries


def _fixed(arguments: Sequence[str], count: int, command: str) -> Sequence[str]:
    if len(arguments) != count:
        raise ResponseError(f"wrong number of arguments for '{command.lower()}' command")
    return arguments


def _at_least(arguments: Sequence[str], count: int, command: str) -> Sequence[str]:
    if len(arguments) < count:
        raise ResponseError(f"wrong number of arguments for '{command.lower()}' command")
    return arguments


def _lua_text(value: object) -> str:
    """Encode one Lua value the way Redis encodes a script's command arguments."""

    if isinstance(value, bool):
        raise ResponseError("Lua redis lib command arguments must be strings or integers")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.17g}"
    raise ResponseError("Lua redis lib command arguments must be strings or integers")


def _wire_text(value: RedisWireScalar) -> str:
    """Encode one EVAL argument the way the redis client encodes a bulk string."""

    if isinstance(value, bool):
        raise DataError("Invalid input of type: 'bool'")
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, str):
        return value
    return repr(value)


def _stored_text(value: RedisWireScalar | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


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
