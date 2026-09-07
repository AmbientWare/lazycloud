from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.http.collections import (
    MAX_MAP_TTL_SECONDS,
    MapCountResponse,
    MapDeleteResponse,
    MapGetResponse,
    MapKeyBody,
    MapKeysResponse,
    MapSetBody,
    MapSetResponse,
    encode_bytes,
)
from shared.http_transport import HttpChannel

from lazycloud.control import workspace_path, workspace_query


class MapControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class MapControlClient:
    channel: MapControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> MapControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def set(
        self,
        name: str,
        key: str,
        value: bytes,
        *,
        ttl_seconds: int = MAX_MAP_TTL_SECONDS,
    ) -> MapSetResponse:
        request = MapSetBody(
            key=key,
            value_base64=encode_bytes(value),
            ttl_seconds=ttl_seconds,
        )
        return MapSetResponse.model_validate(
            self.channel.post(
                self._path(name, "set", workspace=self.workspace),
                request.model_dump(mode="json"),
            )
        )

    def get(self, name: str, key: str) -> MapGetResponse:
        return MapGetResponse.model_validate(
            self.channel.get(self._path(name, "get", workspace=self.workspace, query={"key": key}))
        )

    def delete(self, name: str, key: str) -> MapDeleteResponse:
        request = MapKeyBody(key=key)
        return MapDeleteResponse.model_validate(
            self.channel.post(
                self._path(name, "delete", workspace=self.workspace),
                request.model_dump(mode="json"),
            )
        )

    def count(self, name: str) -> MapCountResponse:
        return MapCountResponse.model_validate(
            self.channel.get(self._path(name, "count", workspace=self.workspace))
        )

    def keys(self, name: str) -> MapKeysResponse:
        return MapKeysResponse.model_validate(
            self.channel.get(self._path(name, "keys", workspace=self.workspace))
        )

    def delete_map(self, name: str) -> None:
        self.channel.delete(self._collection_path(name, workspace=self.workspace))

    def _path(
        self,
        name: str,
        suffix: str,
        *,
        workspace: str,
        query: dict[str, str] | None = None,
    ) -> str:
        params = {**workspace_query(workspace), **(query or {})}
        base = f"/api/v1/maps/{quote(name, safe='')}/{suffix}"
        return f"{base}?{urlencode(params)}" if params else base

    def _collection_path(self, name: str, *, workspace: str) -> str:
        return workspace_path(f"/api/v1/maps/{quote(name, safe='')}", workspace)


__all__ = [
    "MapControlChannel",
    "MapControlClient",
]
