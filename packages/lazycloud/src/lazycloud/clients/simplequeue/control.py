from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.http.collections import (
    SimpleQueueEmptyResponse,
    SimpleQueuePeekResponse,
    SimpleQueuePopResponse,
    SimpleQueuePutBody,
    SimpleQueuePutResponse,
    SimpleQueueSizeResponse,
)
from shared.http_transport import HttpChannel


class SimpleQueueControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class SimpleQueueControlClient:
    channel: SimpleQueueControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> SimpleQueueControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def put(self, name: str, value: bytes) -> SimpleQueuePutResponse:
        request = SimpleQueuePutBody.from_bytes(value)
        return SimpleQueuePutResponse.model_validate(
            self.channel.post(
                self._path(name, "put", workspace=self.workspace),
                request.model_dump(mode="json"),
            )
        )

    def pop(self, name: str) -> SimpleQueuePopResponse:
        return SimpleQueuePopResponse.model_validate(
            self.channel.post(self._path(name, "pop", workspace=self.workspace))
        )

    def peek(self, name: str) -> SimpleQueuePeekResponse:
        return SimpleQueuePeekResponse.model_validate(
            self.channel.get(self._path(name, "peek", workspace=self.workspace))
        )

    def empty(self, name: str) -> SimpleQueueEmptyResponse:
        return SimpleQueueEmptyResponse.model_validate(
            self.channel.get(self._path(name, "empty", workspace=self.workspace))
        )

    def size(self, name: str) -> SimpleQueueSizeResponse:
        return SimpleQueueSizeResponse.model_validate(
            self.channel.get(self._path(name, "size", workspace=self.workspace))
        )

    def delete(self, name: str) -> None:
        self.channel.delete(self._collection_path(name, workspace=self.workspace))

    def _path(self, name: str, suffix: str, *, workspace: str) -> str:
        query = urlencode({"workspace": workspace})
        return f"/api/v1/simplequeues/{quote(name, safe='')}/{suffix}?{query}"

    def _collection_path(self, name: str, *, workspace: str) -> str:
        query = urlencode({"workspace": workspace})
        return f"/api/v1/simplequeues/{quote(name, safe='')}?{query}"


__all__ = [
    "SimpleQueueControlChannel",
    "SimpleQueueControlClient",
]
