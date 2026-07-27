from __future__ import annotations

import re
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis import Redis
from redis.client import Pipeline, PubSub
from redis.exceptions import RedisError
from redis.typing import EncodableT, FieldT, KeyT, StreamIdT
from shared.app_identity import ENV_PREFIX, REDIS_KEY_PREFIX

type RedisWireScalar = str | bytes | int | float | bool
type RedisWireResponse = (
    RedisWireScalar
    | None
    | Sequence[RedisWireResponse]
    | AbstractSet[RedisWireScalar]
    | Mapping[str, RedisWireResponse]
    | Mapping[bytes, RedisWireResponse]
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
    ) -> RedisPubSubMessage | None: ...

    def close(self) -> None: ...


class _RedisPyPubSubProtocol(Protocol):
    def subscribe(self, *channels: str) -> None: ...

    def get_message(
        self,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> Mapping[str, RedisWireResponse] | None: ...

    def close(self) -> None: ...


class _RedisPipelineListPopProtocol(Protocol):
    def lpop(self, name: str) -> RedisCommandResponse: ...


class RedisTransport(Protocol):
    """Frozen synchronous Redis command surface used by production owners."""

    def ping(self, **kwargs: RedisWireScalar) -> RedisCommandResponse: ...

    def close(self) -> RedisCommandResponse: ...

    def get(self, name: str) -> RedisCommandResponse: ...

    def getdel(self, name: str) -> RedisCommandResponse: ...

    def set(
        self,
        name: str,
        value: RedisWireScalar,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> RedisCommandResponse: ...

    def delete(self, *names: str) -> RedisCommandResponse: ...

    def exists(self, *names: str) -> RedisCommandResponse: ...

    def expire(self, name: str, time: int) -> RedisCommandResponse: ...

    def ttl(self, name: str) -> RedisCommandResponse: ...

    def incr(self, name: str) -> RedisCommandResponse: ...

    def decr(self, name: str) -> RedisCommandResponse: ...

    def strlen(self, name: str) -> RedisCommandResponse: ...

    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> RedisCommandResponse: ...

    def hget(self, name: str, key: str) -> RedisCommandResponse: ...

    def hgetall(self, name: str) -> RedisCommandResponse: ...

    def hdel(self, name: str, *keys: str) -> RedisCommandResponse: ...

    def hlen(self, name: str) -> RedisCommandResponse: ...

    def sadd(self, name: str, *values: RedisWireScalar) -> RedisCommandResponse: ...

    def srem(self, name: str, *values: RedisWireScalar) -> RedisCommandResponse: ...

    def smembers(self, name: str) -> RedisCommandResponse: ...

    def scard(self, name: str) -> RedisCommandResponse: ...

    def sismember(self, name: str, value: str) -> RedisCommandResponse: ...

    def rpush(self, name: str, *values: RedisWireScalar) -> RedisCommandResponse: ...

    def lpop(self, name: str) -> RedisCommandResponse: ...

    def blpop(
        self,
        keys: str | list[str],
        *,
        timeout: float,
    ) -> RedisCommandResponse: ...

    def lrange(self, name: str, start: int, end: int) -> RedisCommandResponse: ...

    def lindex(self, name: str, index: int) -> RedisCommandResponse: ...

    def llen(self, name: str) -> RedisCommandResponse: ...

    def zadd(self, name: str, mapping: Mapping[str, float]) -> RedisCommandResponse: ...

    def zrange(self, name: str, start: int, end: int) -> RedisCommandResponse: ...

    def zrangebyscore(
        self,
        name: str,
        min: float | str,
        max: float | str,
    ) -> RedisCommandResponse: ...

    def zscore(self, name: str, value: RedisWireScalar) -> RedisCommandResponse: ...

    def zcard(self, name: str) -> RedisCommandResponse: ...

    def publish(self, channel: str, message: RedisWireScalar) -> RedisCommandResponse: ...

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> RedisCommandResponse: ...

    def xadd(
        self,
        name: str,
        fields: dict[FieldT, EncodableT],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> RedisCommandResponse: ...

    def xrevrange(
        self,
        name: str,
        max: str = "+",
        min: str = "-",
        count: int | None = None,
    ) -> RedisCommandResponse: ...

    def xread(
        self,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> RedisCommandResponse: ...

    def pubsub(
        self,
        *,
        ignore_subscribe_messages: bool = False,
    ) -> PubSub: ...

    def pipeline(
        self,
        transaction: bool = True,
        shard_hint: str | None = None,
    ) -> Pipeline: ...

    def scan_iter(
        self,
        *,
        match: str,
        count: int,
    ) -> Iterable[RedisWireScalar]: ...


REDIS_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (RedisError, OSError)
"""Exception types coordination primitives raise when Redis transport fails.

Callers own outage policy; they catch these to decide fail-safe behavior without
depending on the redis distribution directly.
"""


class RedisSettings(BaseSettings):
    url: str = "redis://localhost:6379/0"
    key_prefix: str = REDIS_KEY_PREFIX
    client_name: str = ""
    decode_responses: bool = True
    socket_timeout_seconds: float = 5.0
    health_check_interval_seconds: int = 30

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_REDIS_",
        extra="ignore",
    )


@dataclass(frozen=True, slots=True)
class RedisConnectionInfo:
    scheme: str
    host: str = ""
    port: int = 0
    database: int = 0
    client_name: str = ""
    socket_path: str = ""


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
class _RedisPyPubSubTransport:
    _pubsub: _RedisPyPubSubProtocol

    def subscribe(self, *channels: str) -> None:
        self._pubsub.subscribe(*channels)

    def get_message(
        self,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,
    ) -> RedisPubSubMessage | None:
        message = self._pubsub.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=timeout,
        )
        if message is None:
            return None
        return _PUBSUB_MESSAGE_ADAPTER.validate_python(message)

    def close(self) -> None:
        self._pubsub.close()


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
        return self._transport.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=timeout,
        )

    def close(self) -> None:
        self._transport.close()


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
        _pipeline_list_pop(self._pipeline, key)

    def execute(self) -> list[RedisWireResponse]:
        return _PIPELINE_RESULTS_ADAPTER.validate_python(self._pipeline.execute())


@dataclass(slots=True)
class RedisClient:
    _transport: RedisTransport
    key_prefix: str = REDIS_KEY_PREFIX
    connection_info: RedisConnectionInfo | None = None

    @classmethod
    def from_settings(
        cls,
        settings: RedisSettings | None = None,
        *,
        decode_responses: bool | None = None,
    ) -> RedisClient:
        config = settings or RedisSettings()
        client_name = sanitize_redis_client_name(config.client_name)
        transport, connection_info = _redis_from_url(
            config.url,
            decode_responses=(
                config.decode_responses if decode_responses is None else decode_responses
            ),
            socket_timeout=config.socket_timeout_seconds,
            health_check_interval=config.health_check_interval_seconds,
            client_name=client_name or None,
        )
        return cls(
            transport,
            key_prefix=config.key_prefix,
            connection_info=connection_info,
        )

    def with_key_prefix(self, key_prefix: str) -> RedisClient:
        return RedisClient(
            self._transport,
            key_prefix=key_prefix,
            connection_info=self.connection_info,
        )

    @property
    def transport_identity(self) -> int:
        return id(self._transport)

    def key(self, *parts: RedisKeyPart) -> str:
        cleaned = [str(part).strip(":") for part in parts if str(part).strip(":")]
        return ":".join([self.key_prefix, *cleaned])

    def ping(self) -> bool:
        return _redis_bool(self._transport.ping(), "PING")

    def set(
        self,
        key: str,
        value: RedisWireScalar,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        raw = self._transport.set(key, value, ex=ex, nx=nx)
        if raw is None:
            return False
        return _redis_bool(raw, "SET")

    def get(self, key: str) -> RedisWireScalar | None:
        return _optional_scalar(self._transport.get(key), "GET")

    def set_single_use(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        """Store a short-lived value only when its key does not already exist."""

        return self.set(key, value, ex=ttl_seconds, nx=True)

    def getdel(self, key: str) -> str | None:
        """Atomically return and remove one short-lived coordination value."""

        value = _optional_scalar(
            self._transport.getdel(key),
            "GETDEL",
        )
        if value is None:
            return None
        return redis_text(value)

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

    def decrement(self, key: str) -> int:
        return _redis_int(self._transport.decr(key), "DECR")

    def string_length(self, key: str) -> int:
        return _redis_int(
            self._transport.strlen(key),
            "STRLEN",
        )

    def hash_set(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        *,
        mapping: Mapping[str, str] | None = None,
    ) -> int:
        return _redis_int(
            self._transport.hset(
                key,
                field,
                value,
                mapping=dict(mapping) if mapping is not None else None,
            ),
            "HSET",
        )

    def hash_get(self, key: str, field: str) -> RedisWireScalar | None:
        return _optional_scalar(
            self._transport.hget(key, field),
            "HGET",
        )

    def hash_get_all(self, key: str) -> dict[RedisWireScalar, RedisWireScalar]:
        raw = _sync_response(self._transport.hgetall(key), "HGETALL")
        if not isinstance(raw, dict):
            raise TypeError("Redis HGETALL response must be a mapping")
        values: dict[RedisWireScalar, RedisWireScalar] = {}
        for raw_key, raw_value in raw.items():
            key_scalar = _scalar(raw_key, "HGETALL key")
            values[key_scalar] = _scalar(raw_value, "HGETALL value")
        return values

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

    def sorted_set_score(self, key: str, member: RedisWireScalar) -> float | None:
        raw = _optional_scalar(
            self._transport.zscore(key, member),
            "ZSCORE",
        )
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise TypeError("Redis ZSCORE response must be numeric")
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(raw)
        except ValueError as exc:
            raise TypeError("Redis ZSCORE response must be numeric") from exc

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
        encoded_fields: dict[FieldT, EncodableT] = {field: value for field, value in fields.items()}
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
    ) -> list[RedisStreamEntry]:
        raw = _sync_response(
            self._transport.xrevrange(
                stream,
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
        raw = _sync_response(
            self._transport.xread(
                stream_offsets,
                count=count,
                block=block,
            ),
            "XREAD",
        )
        if not isinstance(raw, (list, tuple)):
            raise TypeError("Redis XREAD response must be a sequence")
        reads: list[RedisStreamRead] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise TypeError("Redis XREAD stream must contain a name and entries")
            name = _scalar(item[0], "XREAD stream name")
            reads.append((name, _stream_entries(item[1], "XREAD entries")))
        return reads

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> RedisSubscription:
        transport = _RedisPyPubSubTransport(
            self._transport.pubsub(ignore_subscribe_messages=ignore_subscribe_messages)
        )
        return RedisSubscription(transport)

    def pipeline(self, *, transaction: bool) -> RedisPipeline:
        return RedisPipeline(self._transport.pipeline(transaction=transaction))

    def keys(self, pattern: str) -> list[str]:
        return self.scan(pattern)

    def scan(self, pattern: str, *, count: int = 10_000) -> list[str]:
        seen: set[str] = set()
        keys: list[str] = []

        def add(raw_key: RedisWireScalar) -> None:
            key = redis_text(raw_key)
            if key not in seen and fnmatch(key, pattern):
                seen.add(key)
                keys.append(key)

        for key in self._transport.scan_iter(match=pattern, count=count):
            add(key)
        return keys

    def delete_matching(self, pattern: str) -> int:
        return self.delete(*self.scan(pattern))

    def close(self) -> None:
        _sync_response(self._transport.close(), "CLOSE")


def sanitize_redis_client_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "", name.replace(" ", "").replace("\n", ""))


def _pipeline_list_pop(pipeline: _RedisPipelineListPopProtocol, key: str) -> None:
    pipeline.lpop(key)


def _redis_from_url(
    url: str,
    *,
    decode_responses: bool,
    socket_timeout: float,
    health_check_interval: int,
    client_name: str | None,
) -> tuple[Redis, RedisConnectionInfo]:
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
        return (
            Redis(
                unix_socket_path=socket_path,
                db=database,
                username=username,
                password=password,
                decode_responses=decode_responses,
                socket_timeout=socket_timeout,
                health_check_interval=health_check_interval,
                client_name=client_name,
            ),
            RedisConnectionInfo(
                scheme=parsed.scheme,
                database=database,
                client_name=client_name or "",
                socket_path=socket_path,
            ),
        )
    host = unquote(parsed.hostname) if parsed.hostname else "localhost"
    port = parsed.port or 6379
    return (
        Redis(
            host=host,
            port=port,
            db=database,
            username=username,
            password=password,
            ssl=parsed.scheme == "rediss",
            decode_responses=decode_responses,
            socket_timeout=socket_timeout,
            health_check_interval=health_check_interval,
            client_name=client_name,
        ),
        RedisConnectionInfo(
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


def _stream_entries(value: RedisCommandResponse, operation: str) -> list[RedisStreamEntry]:
    value = _sync_response(value, operation)
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Redis {operation} response must be a sequence")
    entries: list[RedisStreamEntry] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TypeError(f"Redis {operation} entry must contain an id and fields")
        entry_id = _scalar(item[0], f"{operation} entry id")
        raw_fields = item[1]
        if not isinstance(raw_fields, dict):
            raise TypeError(f"Redis {operation} fields must be a mapping")
        fields: dict[RedisWireScalar, RedisWireScalar] = {}
        for raw_key, raw_value in raw_fields.items():
            fields[_scalar(raw_key, f"{operation} field key")] = _scalar(
                raw_value,
                f"{operation} field value",
            )
        entries.append((entry_id, fields))
    return entries
