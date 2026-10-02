from __future__ import annotations

import base64
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from lazycloud.clients.api import ApiError
from lazycloud.clients.storage import StorageClient
from lazycloud.contracts.api import ErrorCode, SetMapEntryRequest
from lazycloud.control import (
    ResourceControlBinding,
    storage_client,
)
from lazycloud.values import decode_value, encode_value

MAX_MAP_TTL_SECONDS = 7 * 24 * 60 * 60
_MISSING = object()


class MapSetError(ValueError):
    pass


@dataclass(slots=True)
class Map(ResourceControlBinding[StorageClient], MutableMapping[str, Any]):
    """A dictionary of Python values with per-key expiry, created on the first write."""

    name: str
    workspace: str | None = None
    client: StorageClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> StorageClient:
        if self.client is None:
            self.client = storage_client(self._config())
        return self.client

    def set(
        self,
        key: str,
        value: Any,
        ttl: int = MAX_MAP_TTL_SECONDS,
    ) -> bool:
        """Write a key that expires after `ttl` seconds, at most 7 days; 0 never expires."""
        request = SetMapEntryRequest(
            value=base64.b64encode(encode_value(value)), ttl_seconds=_normalize_ttl(ttl)
        )
        self.control_client.set_map_entry(self.name, key, request)
        return True

    def get(self, key: str, default: Any = None) -> Any:
        try:
            entry = self.control_client.get_map_entry(self.name, key)
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                return default
            raise
        return decode_value(entry.value)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        try:
            self.control_client.delete_map_entry(self.name, key)
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                raise KeyError(key) from exc
            raise

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key, _MISSING) is not _MISSING

    def __iter__(self) -> Iterator[str]:
        cursor: str | None = None
        while True:
            page = self.control_client.list_map_keys(self.name, cursor=cursor)
            yield from page.keys
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def __len__(self) -> int:
        return self.control_client.get_map(self.name).count

    def delete(self) -> None:
        self.control_client.delete_map(self.name)


def _normalize_ttl(value: int) -> int:
    if value < 0:
        msg = "map item ttl must be non-negative"
        raise ValueError(msg)
    if value > MAX_MAP_TTL_SECONDS:
        msg = f"map item ttl cannot exceed {MAX_MAP_TTL_SECONDS} seconds"
        raise ValueError(msg)
    return value


__all__ = [
    "MAX_MAP_TTL_SECONDS",
    "Map",
    "MapSetError",
]
