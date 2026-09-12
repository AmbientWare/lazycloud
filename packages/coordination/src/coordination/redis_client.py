from __future__ import annotations

import re
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Literal, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import Field, TypeAdapter, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.connection import (
    AbstractConnection as AsyncAbstractConnection,
)
from redis.asyncio.connection import (
    Connection as AsyncConnection,
)
from redis.asyncio.connection import (
    ConnectionPool as AsyncConnectionPool,
)
from redis.asyncio.connection import (
    SSLConnection as AsyncSSLConnection,
)
from redis.asyncio.connection import (
    UnixDomainSocketConnection as AsyncUnixDomainSocketConnection,
)
from redis.client import Pipeline
from redis.connection import Connection as SyncConnection
from redis.connection import ConnectionInterface as SyncConnectionInterface
from redis.connection import ConnectionPool as SyncConnectionPool
from redis.connection import SSLConnection as SyncSSLConnection
from redis.exceptions import MaxConnectionsError, RedisError
from redis.typing import EncodableT, FieldT, KeyT, StreamIdT
from shared.app_identity import ENV_PREFIX, REDIS_KEY_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError

type RedisWireScalar = str | bytes | int | float | bool
# Mapping keys are invariant, so a decoded `str`-keyed reply and the `bytes | str`
# reply redis-py declares for a command are separate arms; neither widens the other.
type RedisWireResponse = (
    RedisWireScalar
    | Sequence[RedisWireResponse]
    | AbstractSet[RedisWireScalar]
    | Mapping[str, RedisWireResponse]
    | Mapping[bytes | str, RedisWireResponse]
    | None
)
type RedisCommandResponse = RedisWireResponse | Awaitable[RedisWireResponse]
type RedisKeyPart = str | int
type RedisStreamEntry = tuple[RedisWireScalar, dict[RedisWireScalar, RedisWireScalar]]
type RedisStreamRead = tuple[RedisWireScalar, list[RedisStreamEntry]]


class RedisPubSubTransport(Protocol):
    def subscribe(self, *channels: str) -> None: ...

    def get_message(
        self,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> Mapping[str, RedisWireResponse] | None: ...

    def close(self) -> None: ...


class RedisCommandTransport[CommandResponseT](Protocol):
    def ping(self, **kwargs: RedisWireScalar) -> CommandResponseT: ...

    def get(self, name: str) -> CommandResponseT: ...

    def mget(self, keys: Iterable[str]) -> CommandResponseT: ...

    def getdel(self, name: str) -> CommandResponseT: ...

    def set(
        self,
        name: str,
        value: RedisWireScalar,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> CommandResponseT: ...

    def delete(self, *names: str) -> CommandResponseT: ...

    def exists(self, *names: str) -> CommandResponseT: ...

    def expire(self, name: str, time: int) -> CommandResponseT: ...

    def ttl(self, name: str) -> CommandResponseT: ...

    def incr(self, name: str) -> CommandResponseT: ...

    def strlen(self, name: str) -> CommandResponseT: ...

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: Mapping[FieldT, EncodableT] | None = None,
    ) -> CommandResponseT: ...

    def hget(self, name: str, key: str) -> CommandResponseT: ...

    def hgetall(self, name: str) -> CommandResponseT: ...

    def hdel(self, name: str, *keys: str) -> CommandResponseT: ...

    def hlen(self, name: str) -> CommandResponseT: ...

    def sadd(self, name: str, *values: RedisWireScalar) -> CommandResponseT: ...

    def srem(self, name: str, *values: RedisWireScalar) -> CommandResponseT: ...

    def smembers(self, name: str) -> CommandResponseT: ...

    def scard(self, name: str) -> CommandResponseT: ...

    def sismember(self, name: str, value: str) -> CommandResponseT: ...

    def rpush(self, name: str, *values: RedisWireScalar) -> CommandResponseT: ...

    def lpop(self, name: str) -> CommandResponseT: ...

    def blpop(
        self,
        keys: str | list[str],
        *,
        timeout: float,
    ) -> CommandResponseT: ...

    def blmove(
        self,
        first_list: str,
        second_list: str,
        timeout: int,
    ) -> CommandResponseT: ...

    def lrange(self, name: str, start: int, end: int) -> CommandResponseT: ...

    def lindex(self, name: str, index: int) -> CommandResponseT: ...

    def llen(self, name: str) -> CommandResponseT: ...

    def zadd(self, name: str, mapping: Mapping[str, float]) -> CommandResponseT: ...

    def zrange(self, name: str, start: int, end: int) -> CommandResponseT: ...

    def zrangebyscore(
        self,
        name: str,
        min: float | str,
        max: float | str,
    ) -> CommandResponseT: ...

    def zcard(self, name: str) -> CommandResponseT: ...

    def publish(self, channel: str, message: RedisWireScalar) -> CommandResponseT: ...

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> CommandResponseT: ...

    def xadd(
        self,
        name: str,
        fields: dict[FieldT, EncodableT],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> CommandResponseT: ...

    def xrevrange(
        self,
        name: str,
        max: str = "+",
        min: str = "-",
        count: int | None = None,
    ) -> CommandResponseT: ...

    def xread(
        self,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> CommandResponseT: ...

    def scan(
        self,
        cursor: int,
        *,
        match: str,
        count: int,
    ) -> CommandResponseT: ...


class RedisTransport(RedisCommandTransport[RedisCommandResponse], Protocol):
    def close(self) -> RedisCommandResponse: ...

    def pubsub(
        self,
        *,
        ignore_subscribe_messages: bool = False,
    ) -> RedisPubSubTransport: ...

    def pipeline(
        self,
        transaction: bool = True,
        shard_hint: str | None = None,
    ) -> Pipeline: ...


class AsyncRedisPubSubTransport(Protocol):
    async def subscribe(self, *channels: str) -> None: ...

    async def get_message(
        self,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> Mapping[str, RedisWireResponse] | None: ...

    async def aclose(self) -> None: ...


class AsyncRedisTransport(
    RedisCommandTransport[Awaitable[RedisWireResponse]],
    Protocol,
):
    # redis-py types every command as `Awaitable | Any`, so a sync client would
    # satisfy the awaitable arm structurally; this attribute is what tells them apart.
    _is_async_client: Literal[True]

    async def aclose(self) -> None: ...

    def pubsub(
        self,
        *,
        ignore_subscribe_messages: bool = False,
    ) -> AsyncRedisPubSubTransport: ...


REDIS_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (RedisError, OSError)
"""Exception types coordination primitives raise when Redis transport fails.

Callers own outage policy; they catch these to decide fail-safe behavior without
depending on the redis distribution directly.
"""


class RedisSettings(BaseSettings):
    # Blank marks "nobody said", not an instance. Which Redis holds a process's
    # queues, locks, and leases is a deployment fact with nothing to fall back to.
    url: str = ""
    key_prefix: str = REDIS_KEY_PREFIX
    client_name: str = ""
    decode_responses: bool = True
    socket_timeout_seconds: float = 5.0
    health_check_interval_seconds: int = 30
    max_connections: int = Field(default=2_048, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_REDIS_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def require_url(self) -> RedisSettings:
        if not self.url.strip():
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_REDIS_URL",
                purpose=(
                    "the Redis instance this process uses for queues, locks, leases, "
                    "and coordination"
                ),
            )
        return self


@dataclass(frozen=True, slots=True)
class RedisConnectionInfo:
    scheme: str
    host: str = ""
    port: int = 0
    database: int = 0
    client_name: str = ""
    socket_path: str = ""


@dataclass(frozen=True, slots=True)
class RedisPoolStatus:
    idle: int
    in_use: int
    capacity: int
    exhaustions_total: int = 0

    @property
    def exhausted(self) -> bool:
        return self.idle == 0 and self.in_use >= self.capacity


@dataclass(frozen=True, slots=True)
class RedisPubSubMessage:
    type: str | bytes
    pattern: str | bytes | None
    channel: str | bytes
    data: RedisWireScalar


_PUBSUB_MESSAGE_ADAPTER: TypeAdapter[RedisPubSubMessage] = TypeAdapter(RedisPubSubMessage)
_PIPELINE_RESULTS_ADAPTER: TypeAdapter[list[RedisWireResponse]] = TypeAdapter(
    list[RedisWireResponse]
)


@dataclass(slots=True)
class RedisSubscription:
    _transport: RedisPubSubTransport

    def subscribe(self, *channels: str) -> None:
        self._transport.subscribe(*channels)

    def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> RedisPubSubMessage | None:
        message = self._transport.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=timeout,
        )
        if message is None:
            return None
        return _PUBSUB_MESSAGE_ADAPTER.validate_python(message)

    def close(self) -> None:
        self._transport.close()


@dataclass(slots=True)
class AsyncRedisSubscription:
    _transport: AsyncRedisPubSubTransport

    async def subscribe(self, *channels: str) -> None:
        await self._transport.subscribe(*channels)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> RedisPubSubMessage | None:
        message = await self._transport.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=timeout,
        )
        if message is None:
            return None
        return _PUBSUB_MESSAGE_ADAPTER.validate_python(message)

    async def close(self) -> None:
        await self._transport.aclose()


@dataclass(slots=True)
class RedisPipeline:
    _pipeline: Pipeline

    def set(self, key: str, value: RedisWireScalar, *, ex: int | None = None) -> None:
        self._pipeline.set(key, value, ex=ex)

    def delete(self, *keys: str) -> None:
        self._pipeline.delete(*keys)

    def set_add(self, key: str, *values: RedisWireScalar) -> None:
        self._pipeline.sadd(key, *values)

    def set_remove(self, key: str, *values: RedisWireScalar) -> None:
        self._pipeline.srem(key, *values)

    def list_push(self, key: str, *values: RedisWireScalar) -> None:
        self._pipeline.rpush(key, *values)

    def sorted_set_add(self, key: str, mapping: Mapping[str, float]) -> None:
        self._pipeline.zadd(key, mapping)

    def sorted_set_remove_by_score(
        self,
        key: str,
        minimum: float | str,
        maximum: float | str,
    ) -> None:
        self._pipeline.zremrangebyscore(key, minimum, maximum)

    def expire(self, key: str, ttl_seconds: int) -> None:
        self._pipeline.expire(key, ttl_seconds)

    def string_length(self, key: str) -> None:
        self._pipeline.strlen(key)

    def ttl(self, key: str) -> None:
        self._pipeline.ttl(key)

    def list_pop(self, key: str) -> None:
        self._pipeline.lpop(key)

    def execute(self) -> list[RedisWireResponse]:
        return _PIPELINE_RESULTS_ADAPTER.validate_python(self._pipeline.execute())


@dataclass(slots=True)
class RedisClient:
    _transport: RedisTransport
    key_prefix: str = REDIS_KEY_PREFIX
    connection_info: RedisConnectionInfo | None = None
    _connection_pool: _MeasuredSyncConnectionPool | None = None

    @classmethod
    def from_settings(
        cls,
        settings: RedisSettings | None = None,
        *,
        decode_responses: bool | None = None,
    ) -> RedisClient:
        config = settings or RedisSettings()
        client_name = sanitize_redis_client_name(config.client_name)
        transport, pool, connection_info = _redis_from_url(
            config.url,
            decode_responses=(
                config.decode_responses if decode_responses is None else decode_responses
            ),
            socket_timeout=config.socket_timeout_seconds,
            health_check_interval=config.health_check_interval_seconds,
            client_name=client_name or None,
            max_connections=config.max_connections,
        )
        return cls(
            transport,
            key_prefix=config.key_prefix,
            connection_info=connection_info,
            _connection_pool=pool,
        )

    def with_key_prefix(self, key_prefix: str) -> RedisClient:
        return RedisClient(
            self._transport,
            key_prefix=key_prefix,
            connection_info=self.connection_info,
            _connection_pool=self._connection_pool,
        )

    @property
    def transport_identity(self) -> int:
        return id(self._transport)

    def key(self, *parts: RedisKeyPart) -> str:
        return _prefixed_key(self.key_prefix, parts)

    def ping(self) -> bool:
        return _redis_bool(self._transport.ping(), "PING")

    def set(
        self,
        key: str,
        value: RedisWireScalar,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        raw = self._transport.set(key, value, ex=ex, px=px, nx=nx)
        if raw is None:
            return False
        return _redis_bool(raw, "SET")

    def get(self, key: str) -> RedisWireScalar | None:
        return _optional_scalar(self._transport.get(key), "GET")

    def mget(self, keys: Sequence[str]) -> list[RedisWireScalar | None]:
        if not keys:
            return []
        return _optional_scalars(self._transport.mget(list(keys)), "MGET")

    def set_single_use(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        return self.set(key, value, ex=ttl_seconds, nx=True)

    def getdel(self, key: str) -> str | None:
        value = _optional_scalar(self._transport.getdel(key), "GETDEL")
        return None if value is None else redis_text(value)

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return _redis_int(
            self._transport.delete(*keys),
            "DEL",
        )

    def exists(self, key: str) -> bool:
        return (
            _redis_int(
                self._transport.exists(key),
                "EXISTS",
            )
            > 0
        )

    def expire(self, key: str, ttl_seconds: int) -> bool:
        return _redis_bool(
            self._transport.expire(key, ttl_seconds),
            "EXPIRE",
        )

    def ttl(self, key: str) -> int:
        return _redis_int(self._transport.ttl(key), "TTL")

    def increment(self, key: str) -> int:
        return _redis_int(self._transport.incr(key), "INCR")

    def string_length(self, key: str) -> int:
        return _redis_int(
            self._transport.strlen(key),
            "STRLEN",
        )

    def hash_set(
        self,
        key: str,
        field_name: str | None = None,
        value: str | None = None,
        *,
        mapping: Mapping[str, str] | None = None,
    ) -> int:
        encoded_mapping: dict[FieldT, EncodableT] | None = (
            {name: item for name, item in mapping.items()} if mapping is not None else None
        )
        return _redis_int(
            self._transport.hset(key, field_name, value, mapping=encoded_mapping),
            "HSET",
        )

    def hash_get(self, key: str, field: str) -> RedisWireScalar | None:
        return _optional_scalar(
            self._transport.hget(key, field),
            "HGET",
        )

    def hash_get_all(self, key: str) -> dict[RedisWireScalar, RedisWireScalar]:
        return _scalar_mapping(self._transport.hgetall(key), "HGETALL")

    def hash_delete(self, key: str, *fields: str) -> int:
        return _redis_int(
            self._transport.hdel(key, *fields),
            "HDEL",
        )

    def hash_length(self, key: str) -> int:
        return _redis_int(self._transport.hlen(key), "HLEN")

    def set_add(self, key: str, *values: RedisWireScalar) -> int:
        return _redis_int(
            self._transport.sadd(key, *values),
            "SADD",
        )

    def set_remove(self, key: str, *values: RedisWireScalar) -> int:
        return _redis_int(
            self._transport.srem(key, *values),
            "SREM",
        )

    def set_members(self, key: str) -> set[RedisWireScalar]:
        return set(
            _scalar_sequence(
                self._transport.smembers(key),
                "SMEMBERS",
            )
        )

    def set_cardinality(self, key: str) -> int:
        return _redis_int(
            self._transport.scard(key),
            "SCARD",
        )

    def set_contains(self, key: str, value: str) -> bool:
        return _redis_bool(
            self._transport.sismember(key, value),
            "SISMEMBER",
        )

    def list_push(self, key: str, *values: RedisWireScalar) -> int:
        return _redis_int(
            self._transport.rpush(key, *values),
            "RPUSH",
        )

    def list_pop(self, key: str) -> RedisWireScalar | None:
        return _optional_scalar(
            self._transport.lpop(key),
            "LPOP",
        )

    def blocking_list_pop(
        self,
        keys: str | list[str],
        *,
        timeout: float,
    ) -> tuple[RedisWireScalar, RedisWireScalar] | None:
        raw = self._transport.blpop(keys, timeout=timeout)
        if raw is None:
            return None
        values = _scalar_sequence(raw, "BLPOP")
        if len(values) != 2:
            raise TypeError("Redis BLPOP response must contain a key and value")
        return values[0], values[1]

    def blocking_list_move(
        self,
        source: str,
        destination: str,
        *,
        timeout_seconds: int,
    ) -> RedisWireScalar | None:
        """Move the head of `source` to the tail of `destination`, waiting for one to arrive.

        One command, so a consumer that dies mid-take leaves the entry on
        `destination` rather than nowhere: a blocking pop followed by a push has a
        window in which the value exists only in the caller's memory.

        Whole seconds, and never zero, because Redis reads a zero timeout as "wait
        forever". A caller with less than a second left polls instead of blocking.
        """

        if timeout_seconds < 1:
            raise ValueError("blocking list move timeout must be at least one second")
        return _optional_scalar(
            self._transport.blmove(source, destination, timeout_seconds),
            "BLMOVE",
        )

    def list_range(self, key: str, start: int, end: int) -> list[RedisWireScalar]:
        return _scalar_sequence(
            self._transport.lrange(key, start, end),
            "LRANGE",
        )

    def list_index(self, key: str, index: int) -> RedisWireScalar | None:
        return _optional_scalar(
            self._transport.lindex(key, index),
            "LINDEX",
        )

    def list_length(self, key: str) -> int:
        return _redis_int(self._transport.llen(key), "LLEN")

    def sorted_set_add(self, key: str, mapping: Mapping[str, float]) -> int:
        return _redis_int(
            self._transport.zadd(key, mapping),
            "ZADD",
        )

    def sorted_set_range(self, key: str, start: int, end: int) -> list[RedisWireScalar]:
        return _scalar_sequence(
            self._transport.zrange(key, start, end),
            "ZRANGE",
        )

    def sorted_set_range_by_score(
        self,
        key: str,
        minimum: float | str,
        maximum: float | str,
    ) -> list[RedisWireScalar]:
        return _scalar_sequence(
            self._transport.zrangebyscore(
                key,
                minimum,
                maximum,
            ),
            "ZRANGEBYSCORE",
        )

    def sorted_set_cardinality(self, key: str) -> int:
        return _redis_int(
            self._transport.zcard(key),
            "ZCARD",
        )

    def publish(self, channel: str, message: RedisWireScalar) -> int:
        return _redis_int(
            self._transport.publish(channel, message),
            "PUBLISH",
        )

    def _eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> RedisWireResponse:
        return _sync_response(
            self._transport.eval(
                script,
                numkeys,
                *keys_and_args,
            ),
            "EVAL",
        )

    def eval_int(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> int:
        return _redis_int(self._eval(script, numkeys, *keys_and_args), "EVAL")

    def eval_scalar(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> RedisWireScalar | None:
        return _optional_scalar(self._eval(script, numkeys, *keys_and_args), "EVAL")

    def eval_scalars(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> list[RedisWireScalar]:
        return _scalar_sequence(self._eval(script, numkeys, *keys_and_args), "EVAL")

    def stream_add(
        self,
        stream: str,
        fields: Mapping[str, RedisWireScalar],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> RedisWireScalar:
        encoded_fields: dict[FieldT, EncodableT] = {
            field_name: value for field_name, value in fields.items()
        }
        return _scalar(
            self._transport.xadd(
                stream,
                encoded_fields,
                id=id,
                maxlen=maxlen,
                approximate=approximate,
            ),
            "XADD",
        )

    def stream_reverse_range(
        self,
        stream: str,
        *,
        count: int | None = None,
        before: str | None = None,
    ) -> list[RedisStreamEntry]:
        raw = _sync_response(
            self._transport.xrevrange(
                stream,
                max=f"({before}" if before is not None else "+",
                count=count,
            ),
            "XREVRANGE",
        )
        return _stream_entries(raw, "XREVRANGE")

    def stream_read(
        self,
        streams: Mapping[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> list[RedisStreamRead]:
        stream_offsets: dict[KeyT, StreamIdT] = {
            stream: offset for stream, offset in streams.items()
        }
        return _stream_reads(self._transport.xread(stream_offsets, count=count, block=block))

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> RedisSubscription:
        return RedisSubscription(
            self._transport.pubsub(ignore_subscribe_messages=ignore_subscribe_messages)
        )

    def pipeline(self, *, transaction: bool) -> RedisPipeline:
        return RedisPipeline(self._transport.pipeline(transaction=transaction))

    def scan(self, pattern: str, *, count: int = 10_000) -> list[str]:
        keys: dict[str, None] = {}
        cursor = 0
        while True:
            cursor, page = _scan_page(self._transport.scan(cursor, match=pattern, count=count))
            _add_matching_keys(keys, page, pattern)
            if cursor == 0:
                return list(keys)

    def delete_matching(self, pattern: str) -> int:
        return self.delete(*self.scan(pattern))

    def close(self) -> None:
        _sync_response(self._transport.close(), "CLOSE")

    def pool_status(self) -> RedisPoolStatus | None:
        pool = self._connection_pool
        return None if pool is None else pool.status_snapshot()


@dataclass(slots=True)
class AsyncRedisClient:
    _transport: AsyncRedisTransport
    key_prefix: str = REDIS_KEY_PREFIX
    connection_info: RedisConnectionInfo | None = None
    _connection_pool: _MeasuredAsyncConnectionPool | None = None

    @classmethod
    def from_settings(
        cls,
        settings: RedisSettings | None = None,
        *,
        decode_responses: bool | None = None,
    ) -> AsyncRedisClient:
        config = settings or RedisSettings()
        client_name = sanitize_redis_client_name(config.client_name)
        transport, pool, connection_info = _async_redis_from_url(
            config.url,
            decode_responses=(
                config.decode_responses if decode_responses is None else decode_responses
            ),
            socket_timeout=config.socket_timeout_seconds,
            health_check_interval=config.health_check_interval_seconds,
            client_name=client_name or None,
            max_connections=config.max_connections,
        )
        return cls(
            transport,
            key_prefix=config.key_prefix,
            connection_info=connection_info,
            _connection_pool=pool,
        )

    def key(self, *parts: RedisKeyPart) -> str:
        return _prefixed_key(self.key_prefix, parts)

    async def set(
        self,
        key: str,
        value: RedisWireScalar,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        raw = await self._transport.set(key, value, ex=ex, px=px, nx=nx)
        if raw is None:
            return False
        return _redis_bool(raw, "SET")

    async def get(self, key: str) -> RedisWireScalar | None:
        return _optional_scalar(await self._transport.get(key), "GET")

    async def mget(self, keys: Sequence[str]) -> list[RedisWireScalar | None]:
        if not keys:
            return []
        return _optional_scalars(await self._transport.mget(list(keys)), "MGET")

    async def getdel(self, key: str) -> str | None:
        value = _optional_scalar(await self._transport.getdel(key), "GETDEL")
        return None if value is None else redis_text(value)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return _redis_int(await self._transport.delete(*keys), "DEL")

    async def exists(self, key: str) -> bool:
        return _redis_int(await self._transport.exists(key), "EXISTS") > 0

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        return _redis_bool(await self._transport.expire(key, ttl_seconds), "EXPIRE")

    async def ttl(self, key: str) -> int:
        return _redis_int(await self._transport.ttl(key), "TTL")

    async def increment(self, key: str) -> int:
        return _redis_int(await self._transport.incr(key), "INCR")

    async def hash_set(
        self,
        key: str,
        field_name: str | None = None,
        value: str | None = None,
        *,
        mapping: Mapping[str, str] | None = None,
    ) -> int:
        encoded_mapping: dict[FieldT, EncodableT] | None = (
            {name: item for name, item in mapping.items()} if mapping is not None else None
        )
        return _redis_int(
            await self._transport.hset(key, field_name, value, mapping=encoded_mapping),
            "HSET",
        )

    async def hash_get_all(self, key: str) -> dict[RedisWireScalar, RedisWireScalar]:
        return _scalar_mapping(await self._transport.hgetall(key), "HGETALL")

    async def set_add(self, key: str, *values: RedisWireScalar) -> int:
        return _redis_int(await self._transport.sadd(key, *values), "SADD")

    async def set_remove(self, key: str, *values: RedisWireScalar) -> int:
        return _redis_int(await self._transport.srem(key, *values), "SREM")

    async def set_members(self, key: str) -> set[RedisWireScalar]:
        return set(_scalar_sequence(await self._transport.smembers(key), "SMEMBERS"))

    async def blocking_list_move(
        self,
        source: str,
        destination: str,
        *,
        timeout_seconds: int,
    ) -> RedisWireScalar | None:
        if timeout_seconds < 1:
            raise ValueError("blocking list move timeout must be at least one second")
        return _optional_scalar(
            await self._transport.blmove(source, destination, timeout_seconds),
            "BLMOVE",
        )

    async def publish(self, channel: str, message: RedisWireScalar) -> int:
        return _redis_int(await self._transport.publish(channel, message), "PUBLISH")

    async def _eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> RedisWireResponse:
        return await self._transport.eval(script, numkeys, *keys_and_args)

    async def eval_int(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> int:
        return _redis_int(await self._eval(script, numkeys, *keys_and_args), "EVAL")

    async def eval_scalar(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> RedisWireScalar | None:
        return _optional_scalar(await self._eval(script, numkeys, *keys_and_args), "EVAL")

    async def eval_scalars(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> list[RedisWireScalar]:
        return _scalar_sequence(await self._eval(script, numkeys, *keys_and_args), "EVAL")

    async def stream_add(
        self,
        stream: str,
        fields: Mapping[str, RedisWireScalar],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> RedisWireScalar:
        encoded_fields: dict[FieldT, EncodableT] = {
            field_name: value for field_name, value in fields.items()
        }
        return _scalar(
            await self._transport.xadd(
                stream,
                encoded_fields,
                id=id,
                maxlen=maxlen,
                approximate=approximate,
            ),
            "XADD",
        )

    async def stream_reverse_range(
        self,
        stream: str,
        *,
        count: int | None = None,
    ) -> list[RedisStreamEntry]:
        raw = await self._transport.xrevrange(stream, count=count)
        return _stream_entries(raw, "XREVRANGE")

    async def stream_read(
        self,
        streams: Mapping[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> list[RedisStreamRead]:
        stream_offsets: dict[KeyT, StreamIdT] = {
            stream: offset for stream, offset in streams.items()
        }
        return _stream_reads(await self._transport.xread(stream_offsets, count=count, block=block))

    async def list_length(self, key: str) -> int:
        return _redis_int(await self._transport.llen(key), "LLEN")

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> AsyncRedisSubscription:
        return AsyncRedisSubscription(
            self._transport.pubsub(ignore_subscribe_messages=ignore_subscribe_messages)
        )

    async def scan(self, pattern: str, *, count: int = 10_000) -> list[str]:
        keys: dict[str, None] = {}
        cursor = 0
        while True:
            cursor, page = _scan_page(
                await self._transport.scan(cursor, match=pattern, count=count)
            )
            _add_matching_keys(keys, page, pattern)
            if cursor == 0:
                return list(keys)

    async def close(self) -> None:
        await self._transport.aclose()

    def pool_status(self) -> RedisPoolStatus | None:
        pool = self._connection_pool
        return None if pool is None else pool.status_snapshot()


class _MeasuredSyncConnectionPool(SyncConnectionPool):
    exhaustions_total: int = 0
    _available_connections: list[SyncConnectionInterface]
    _in_use_connections: set[SyncConnectionInterface]

    def make_connection(self) -> SyncConnectionInterface:
        try:
            return super().make_connection()
        except MaxConnectionsError:
            self.exhaustions_total += 1
            raise

    def status_snapshot(self) -> RedisPoolStatus:
        return RedisPoolStatus(
            idle=len(self._available_connections),
            in_use=len(self._in_use_connections),
            capacity=self.max_connections,
            exhaustions_total=self.exhaustions_total,
        )


class _MeasuredAsyncConnectionPool(AsyncConnectionPool):
    exhaustions_total: int = 0
    _available_connections: list[AsyncAbstractConnection]
    _in_use_connections: set[AsyncAbstractConnection]

    def get_available_connection(self) -> AsyncAbstractConnection:
        try:
            return super().get_available_connection()
        except MaxConnectionsError:
            self.exhaustions_total += 1
            raise

    def status_snapshot(self) -> RedisPoolStatus:
        return RedisPoolStatus(
            idle=len(self._available_connections),
            in_use=len(self._in_use_connections),
            capacity=self.max_connections,
            exhaustions_total=self.exhaustions_total,
        )


def sanitize_redis_client_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "", name)


def _redis_from_url(
    url: str,
    *,
    decode_responses: bool,
    socket_timeout: float,
    health_check_interval: int,
    client_name: str | None,
    max_connections: int,
) -> tuple[Redis, _MeasuredSyncConnectionPool | None, RedisConnectionInfo]:
    plan = _redis_connection_plan(url, client_name=client_name)
    if plan.scheme == "unix":
        # redis-py types the sync pool's connection_class as `Connection`, which the
        # Unix socket connection is not, so that pool is built by Redis itself and
        # stays unmeasured.
        transport = Redis(
            unix_socket_path=plan.socket_path,
            db=plan.database,
            username=plan.username,
            password=plan.password,
            decode_responses=decode_responses,
            socket_timeout=socket_timeout,
            health_check_interval=health_check_interval,
            client_name=client_name,
            max_connections=max_connections,
        )
        return transport, None, plan.info
    pool = _MeasuredSyncConnectionPool(
        connection_class=(SyncSSLConnection if plan.scheme == "rediss" else SyncConnection),
        host=plan.host,
        port=plan.port,
        db=plan.database,
        username=plan.username,
        password=plan.password,
        decode_responses=decode_responses,
        socket_timeout=socket_timeout,
        health_check_interval=health_check_interval,
        client_name=client_name,
        max_connections=max_connections,
    )
    return Redis(connection_pool=pool), pool, plan.info


def _async_redis_from_url(
    url: str,
    *,
    decode_responses: bool,
    socket_timeout: float,
    health_check_interval: int,
    client_name: str | None,
    max_connections: int,
) -> tuple[AsyncRedis, _MeasuredAsyncConnectionPool, RedisConnectionInfo]:
    plan = _redis_connection_plan(url, client_name=client_name)
    pool = (
        _MeasuredAsyncConnectionPool(
            connection_class=AsyncUnixDomainSocketConnection,
            path=plan.socket_path,
            db=plan.database,
            username=plan.username,
            password=plan.password,
            decode_responses=decode_responses,
            socket_timeout=socket_timeout,
            health_check_interval=health_check_interval,
            client_name=client_name,
            max_connections=max_connections,
        )
        if plan.scheme == "unix"
        else _MeasuredAsyncConnectionPool(
            connection_class=(AsyncSSLConnection if plan.scheme == "rediss" else AsyncConnection),
            host=plan.host,
            port=plan.port,
            db=plan.database,
            username=plan.username,
            password=plan.password,
            decode_responses=decode_responses,
            socket_timeout=socket_timeout,
            health_check_interval=health_check_interval,
            client_name=client_name,
            max_connections=max_connections,
        )
    )
    return AsyncRedis(connection_pool=pool), pool, plan.info


@dataclass(frozen=True, slots=True)
class _RedisConnectionPlan:
    scheme: str
    database: int
    username: str | None
    password: str | None = field(repr=False)
    info: RedisConnectionInfo
    host: str = ""
    port: int = 0
    socket_path: str = ""


def _redis_connection_plan(url: str, *, client_name: str | None) -> _RedisConnectionPlan:
    parsed = urlsplit(url)
    if parsed.scheme not in {"redis", "rediss", "unix"}:
        raise ValueError("Redis URL scheme must be redis, rediss, or unix")
    if parsed.fragment:
        raise ValueError("Redis URL fragments are not supported")
    query = parse_qs(parsed.query, keep_blank_values=True)
    unsupported = sorted(set(query) - {"db"})
    if unsupported:
        raise ValueError("Redis URL query options are not supported; use explicit Redis settings")
    query_db = query.get("db")
    if query_db is not None and len(query_db) != 1:
        raise ValueError("Redis URL must contain at most one database option")
    db_text = query_db[0] if query_db else unquote(parsed.path).replace("/", "")
    try:
        database = int(db_text) if db_text else 0
    except ValueError as exc:
        raise ValueError("Redis URL database must be an integer") from exc
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    if parsed.scheme == "unix":
        socket_path = unquote(parsed.path)
        if not socket_path:
            raise ValueError("Unix Redis URL must include a socket path")
        return _RedisConnectionPlan(
            scheme=parsed.scheme,
            database=database,
            username=username,
            password=password,
            socket_path=socket_path,
            info=RedisConnectionInfo(
                scheme=parsed.scheme,
                database=database,
                client_name=client_name or "",
                socket_path=socket_path,
            ),
        )
    host = unquote(parsed.hostname) if parsed.hostname else "localhost"
    port = parsed.port or 6379
    return _RedisConnectionPlan(
        scheme=parsed.scheme,
        database=database,
        username=username,
        password=password,
        host=host,
        port=port,
        info=RedisConnectionInfo(
            scheme=parsed.scheme,
            host=host,
            port=port,
            database=database,
            client_name=client_name or "",
        ),
    )


def redis_text(value: RedisWireScalar) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _prefixed_key(prefix: str, parts: Sequence[RedisKeyPart]) -> str:
    cleaned = [str(part).strip(":") for part in parts if str(part).strip(":")]
    return ":".join([prefix, *cleaned])


def _add_matching_keys(
    keys: dict[str, None],
    page: Sequence[RedisWireScalar],
    pattern: str,
) -> None:
    for raw_key in page:
        key = redis_text(raw_key)
        if fnmatch(key, pattern):
            keys.setdefault(key)


def _sync_response(value: RedisCommandResponse, operation: str) -> RedisWireResponse:
    if isinstance(value, Awaitable):
        raise TypeError(f"Redis {operation} unexpectedly returned an awaitable")
    return value


def _scalar(value: RedisCommandResponse, operation: str) -> RedisWireScalar:
    value = _sync_response(value, operation)
    if not isinstance(value, (str, bytes, int, float, bool)):
        raise TypeError(f"Redis {operation} response must be a wire scalar")
    return value


def _optional_scalar(
    value: RedisCommandResponse,
    operation: str,
) -> RedisWireScalar | None:
    if value is None:
        return None
    return _scalar(value, operation)


def _redis_int(value: RedisCommandResponse, operation: str) -> int:
    scalar = _scalar(value, operation)
    if isinstance(scalar, bool):
        return int(scalar)
    if isinstance(scalar, int):
        return scalar
    if isinstance(scalar, float):
        if not scalar.is_integer():
            raise TypeError(f"Redis {operation} response must be an integer")
        return int(scalar)
    try:
        return int(scalar)
    except ValueError as exc:
        raise TypeError(f"Redis {operation} response must be an integer") from exc


def _redis_bool(value: RedisCommandResponse, operation: str) -> bool:
    scalar = _scalar(value, operation)
    if isinstance(scalar, bytes):
        scalar = scalar.decode()
    if isinstance(scalar, str):
        if scalar.upper() == "OK":
            return True
        if scalar in {"0", "1"}:
            return scalar == "1"
        raise TypeError(f"Redis {operation} response must be boolean")
    if isinstance(scalar, float) and not scalar.is_integer():
        raise TypeError(f"Redis {operation} response must be boolean")
    return bool(scalar)


def _scalar_sequence(value: RedisCommandResponse, operation: str) -> list[RedisWireScalar]:
    value = _sync_response(value, operation)
    if not isinstance(value, (list, tuple, set)):
        raise TypeError(f"Redis {operation} response must be a sequence")
    return [_scalar(item, operation) for item in value]


def _optional_scalars(
    value: RedisCommandResponse,
    operation: str,
) -> list[RedisWireScalar | None]:
    value = _sync_response(value, operation)
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Redis {operation} response must be a sequence")
    return [_optional_scalar(item, f"{operation} item") for item in value]


def _scalar_mapping(
    value: RedisCommandResponse,
    operation: str,
) -> dict[RedisWireScalar, RedisWireScalar]:
    value = _sync_response(value, operation)
    if not isinstance(value, dict):
        raise TypeError(f"Redis {operation} response must be a mapping")
    return {
        _scalar(raw_key, f"{operation} key"): _scalar(raw_value, f"{operation} value")
        for raw_key, raw_value in value.items()
    }


def _scan_page(value: RedisCommandResponse) -> tuple[int, list[RedisWireScalar]]:
    value = _sync_response(value, "SCAN")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise TypeError("Redis SCAN response must contain a cursor and keys")
    return _redis_int(value[0], "SCAN cursor"), _scalar_sequence(value[1], "SCAN keys")


def _stream_entries(value: RedisCommandResponse, operation: str) -> list[RedisStreamEntry]:
    value = _sync_response(value, operation)
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Redis {operation} response must be a sequence")
    entries: list[RedisStreamEntry] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TypeError(f"Redis {operation} entry must contain an id and fields")
        entry_id = _scalar(item[0], f"{operation} entry id")
        entries.append((entry_id, _scalar_mapping(item[1], f"{operation} fields")))
    return entries


def _stream_reads(value: RedisCommandResponse) -> list[RedisStreamRead]:
    value = _sync_response(value, "XREAD")
    if not isinstance(value, (list, tuple)):
        raise TypeError("Redis XREAD response must be a sequence")
    reads: list[RedisStreamRead] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TypeError("Redis XREAD stream must contain a name and entries")
        name = _scalar(item[0], "XREAD stream name")
        reads.append((name, _stream_entries(item[1], "XREAD entries")))
    return reads
