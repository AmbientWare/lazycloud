from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.errors import NotFoundError
from shared.http.collections import (
    MAX_MAP_TTL_SECONDS,
    MapCountResponse,
    MapDeleteResponse,
    MapGetResponse,
    MapKeysResponse,
    MapSetResponse,
)
from shared.http.errors import HttpApiError
from typing_extensions import Self

from lazycloud.clients.map.control import MapControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.values import decode_value, encode_value

_MISSING = object()


class MapClient(Protocol):
    def set(
        self,
        name: str,
        key: str,
        value: bytes,
        *,
        ttl_seconds: int = MAX_MAP_TTL_SECONDS,
    ) -> MapSetResponse: ...

    def get(self, name: str, key: str) -> MapGetResponse: ...

    def delete(self, name: str, key: str) -> MapDeleteResponse: ...

    def count(self, name: str) -> MapCountResponse: ...

    def keys(self, name: str, *, cursor: str = "", search: str = "") -> MapKeysResponse: ...

    def delete_map(self, name: str) -> None: ...


class MapSetError(ValueError):
    pass


@dataclass(slots=True)
class Map(MutableMapping[str, Any]):
    name: str
    workspace: str | None = None
    client: MapClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> MapClient:
        if self.client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = _default_map_client(config)
        return self.client

    def _bind_control(
        self,
        client: MapClient | None = None,
        *,
        workspace: str | None = None,
        endpoint: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Self:
        self.client = client
        if workspace is not None:
            self.workspace = workspace
        if endpoint is not None:
            self.endpoint = endpoint
        if token is not None:
            self.token = token
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        return self

    def set(
        self,
        key: str,
        value: Any,
        ttl: int = MAX_MAP_TTL_SECONDS,
    ) -> bool:
        effective_ttl = _normalize_ttl(ttl)
        self.control_client.set(
            self.name,
            key,
            encode_value(value),
            ttl_seconds=effective_ttl,
        )
        return True

    def get(self, key: str, default: Any = None) -> Any:
        try:
            response = self.control_client.get(self.name, key)
        except HttpApiError as exc:
            if exc.status_code == 404:
                return default
            raise
        except NotFoundError:
            return default
        return _deserialize(response.bytes_value())

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        if self.get(key, _MISSING) is _MISSING:
            raise KeyError(key)
        self.control_client.delete(self.name, key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key, _MISSING) is not _MISSING

    def __iter__(self) -> Iterator[str]:
        cursor = ""
        seen: set[str] = set()
        while True:
            response = self.control_client.keys(self.name, cursor=cursor)
            for key in response.data:
                if key not in seen:
                    seen.add(key)
                    yield key
            cursor = response.next
            if not cursor:
                return

    def __len__(self) -> int:
        response = self.control_client.count(self.name)
        return response.count

    def delete(self) -> None:
        self.control_client.delete_map(self.name)


def _default_map_client(config: ControlClientConfig) -> MapControlClient:
    return MapControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _normalize_ttl(value: int) -> int:
    if value < 0:
        msg = "map item ttl must be non-negative"
        raise ValueError(msg)
    if value > MAX_MAP_TTL_SECONDS:
        msg = f"map item ttl cannot exceed {MAX_MAP_TTL_SECONDS} seconds"
        raise ValueError(msg)
    return value


def _deserialize(value: bytes) -> Any:
    return decode_value(value)


__all__ = [
    "Map",
    "MapClient",
    "MapSetError",
]
