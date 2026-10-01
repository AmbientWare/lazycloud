from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lazycloud.clients.storage import StorageClient
from lazycloud.control import (
    ResourceControlBinding,
    resolve_control_client_config,
    storage_client,
)
from lazycloud.values import decode_value, encode_value


@dataclass(slots=True)
class Queue(ResourceControlBinding[StorageClient]):
    """A first-in, first-out queue of Python values, created on the first put."""

    name: str
    workspace: str | None = None
    client: StorageClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> StorageClient:
        if self.client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = storage_client(config)
        return self.client

    def __len__(self) -> int:
        return self.control_client.get_queue(self.name).size

    def put(self, value: Any) -> bool:
        self.control_client.put_queue_messages(self.name, [encode_value(value)])
        return True

    def pop(self) -> Any:
        """Remove and return the oldest value; None when the queue is empty."""
        message = self.control_client.pop_queue_message(self.name).message
        return None if message is None else decode_value(message)

    def peek(self) -> Any:
        """The oldest value without removing it; None when the queue is empty."""
        message = self.control_client.peek_queue_message(self.name).message
        return None if message is None else decode_value(message)

    def empty(self) -> bool:
        return len(self) == 0

    def delete(self) -> None:
        self.control_client.delete_queue(self.name)


__all__ = ["Queue"]
