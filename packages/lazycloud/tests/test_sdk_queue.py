from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import cloudpickle
import pytest
from lazycloud.abstractions.queue import Queue
from shared.errors import UpstreamUnavailableError
from shared.http.collections import (
    SimpleQueueEmptyResponse,
    SimpleQueuePeekResponse,
    SimpleQueuePopResponse,
    SimpleQueuePutResponse,
    SimpleQueueSizeResponse,
    encode_bytes,
)


@dataclass
class FakeQueueClient:
    values: dict[str, deque[bytes]] = field(default_factory=dict)
    fail_put: bool = False
    fail_read: bool = False

    def put(self, name: str, value: bytes) -> SimpleQueuePutResponse:
        if self.fail_put:
            raise UpstreamUnavailableError("failed to put queue item")
        self.values.setdefault(name, deque()).append(value)
        return SimpleQueuePutResponse()

    def pop(self, name: str) -> SimpleQueuePopResponse:
        if self.fail_read:
            raise UpstreamUnavailableError("failed to pop queue item")
        queue = self.values.setdefault(name, deque())
        value = queue.popleft() if queue else b""
        return SimpleQueuePopResponse(value_base64=encode_bytes(value))

    def peek(self, name: str) -> SimpleQueuePeekResponse:
        if self.fail_read:
            raise UpstreamUnavailableError("failed to peek queue item")
        queue = self.values.setdefault(name, deque())
        value = queue[0] if queue else b""
        return SimpleQueuePeekResponse(value_base64=encode_bytes(value))

    def empty(self, name: str) -> SimpleQueueEmptyResponse:
        if self.fail_read:
            raise UpstreamUnavailableError("failed to read queue state")
        return SimpleQueueEmptyResponse(empty=not self.values.get(name))

    def size(self, name: str) -> SimpleQueueSizeResponse:
        if self.fail_read:
            raise UpstreamUnavailableError("failed to read queue size")
        return SimpleQueueSizeResponse(size=len(self.values.get(name, ())))

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def test_queue_serializes_python_values_and_uses_fifo_order() -> None:
    client = FakeQueueClient()
    queue = Queue("jobs")._bind_control(client)
    first: dict[str, Any] = {"kind": "train", "shape": (1, 2, 3)}
    second: list[Any] = ["deploy", 7]

    assert queue.put(first) is True
    assert queue.put(second) is True

    assert len(queue) == 2
    assert cloudpickle.loads(client.values["jobs"][0]) == first
    assert queue.peek() == first
    assert len(queue) == 2
    assert queue.empty() is False
    assert queue.pop() == first
    assert queue.pop() == second
    assert queue.pop() is None
    assert queue.empty() is True

    queue.put("temporary")
    queue.delete()
    assert "jobs" not in client.values


@pytest.mark.parametrize("method", ["put", "pop", "peek", "empty"])
def test_queue_raises_typed_errors(method: str) -> None:
    client = FakeQueueClient(fail_put=method == "put", fail_read=method != "put")
    queue = Queue("jobs")._bind_control(client)

    with pytest.raises(UpstreamUnavailableError):
        if method == "put":
            queue.put("value")
        else:
            getattr(queue, method)()


def test_queue_size_failure_raises() -> None:
    queue = Queue("jobs")._bind_control(FakeQueueClient(fail_read=True))

    with pytest.raises(UpstreamUnavailableError):
        len(queue)
