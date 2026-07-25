from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.http.collections import (
    SimpleQueueEmptyResponse,
    SimpleQueuePeekResponse,
    SimpleQueuePopResponse,
    SimpleQueuePutResponse,
    SimpleQueueSizeResponse,
)
from typing_extensions import Self

from lazycloud.clients.simplequeue.control import SimpleQueueControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.values import decode_value, encode_value


class QueueClient(Protocol):
    def put(self, name: str, value: bytes) -> SimpleQueuePutResponse: ...

    def pop(self, name: str) -> SimpleQueuePopResponse: ...

    def peek(self, name: str) -> SimpleQueuePeekResponse: ...

    def empty(self, name: str) -> SimpleQueueEmptyResponse: ...

    def size(self, name: str) -> SimpleQueueSizeResponse: ...

    def delete(self, name: str) -> None: ...


@dataclass(slots=True)
class Queue:
    name: str
    workspace: str | None = None
    client: QueueClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> QueueClient:
        if self.client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = _default_queue_client(config)
        return self.client

    def _bind_control(
        self,
        client: QueueClient | None = None,
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

    def __len__(self) -> int:
        return self.control_client.size(self.name).size

    def put(self, value: Any) -> bool:
        self.control_client.put(self.name, _serialize(value))
        return True

    def pop(self) -> Any:
        response = self.control_client.pop(self.name)
        return _deserialize_queue_value(response.bytes_value())

    def peek(self) -> Any:
        response = self.control_client.peek(self.name)
        return _deserialize_queue_value(response.bytes_value())

    def empty(self) -> bool:
        return self.control_client.empty(self.name).empty

    def delete(self) -> None:
        self.control_client.delete(self.name)


def _default_queue_client(config: ControlClientConfig) -> SimpleQueueControlClient:
    return SimpleQueueControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _serialize(value: Any) -> bytes:
    return encode_value(value)


def _deserialize_queue_value(value: bytes) -> Any:
    return decode_value(value)


__all__ = [
    "Queue",
    "QueueClient",
]
