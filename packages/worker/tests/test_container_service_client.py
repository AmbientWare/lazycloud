from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from foundation.io_utils import OutputMessage
from worker.container_client.control import (
    ContainerServiceClient,
)
from worker.container_client.models import (
    ContainerServiceMethod,
    ContainerServicePayload,
)


@dataclass(slots=True)
class UnaryCall:
    method: ContainerServiceMethod
    request: ContainerServicePayload
    timeout_seconds: float | None


@dataclass(slots=True)
class StreamCall:
    method: ContainerServiceMethod
    request: ContainerServicePayload
    timeout_seconds: float | None


@dataclass(slots=True)
class FakeContainerTransport:
    responses: dict[ContainerServiceMethod, ContainerServicePayload] = field(default_factory=dict)
    streams: dict[
        ContainerServiceMethod,
        Iterable[ContainerServicePayload] | Callable[[], Iterable[ContainerServicePayload]],
    ] = field(default_factory=dict)
    unary_calls: list[UnaryCall] = field(default_factory=list)
    stream_calls: list[StreamCall] = field(default_factory=list)

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload:
        self.unary_calls.append(UnaryCall(method, request, timeout_seconds))
        return self.responses.get(method, {"ok": True})

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContainerServicePayload]:
        self.stream_calls.append(StreamCall(method, request, timeout_seconds))
        source = self.streams.get(method, ())
        return source() if callable(source) else source


def test_container_log_stream_filters_empty_entries_and_emits_keepalives() -> None:
    def delayed_logs() -> Iterable[dict[str, str]]:
        time.sleep(0.02)
        yield {"msg": ""}
        yield {"msg": "line one"}
        yield {"msg": "line two"}

    transport = FakeContainerTransport(
        streams={ContainerServiceMethod.ContainerStreamLogs: delayed_logs}
    )
    messages: list[OutputMessage] = []

    ContainerServiceClient(transport).stream_logs(
        "ctr",
        messages.append,
        keepalive_interval_seconds=0.005,
    )

    assert transport.stream_calls[0].method is ContainerServiceMethod.ContainerStreamLogs
    assert [message.msg for message in messages if message.msg] == ["line one", "line two"]
    assert any(message.msg == "" for message in messages)


def test_container_archive_outputs_progress_errors_and_success() -> None:
    transport = FakeContainerTransport(
        streams={
            ContainerServiceMethod.ContainerArchive: [
                {"progress": 0},
                {"progress": 25},
                {"error_msg": "temporary warning"},
                {"done": True, "success": True},
            ]
        }
    )
    messages: list[OutputMessage] = []

    ContainerServiceClient(transport).archive("ctr", "image-1", messages.append)

    assert transport.stream_calls[0].method is ContainerServiceMethod.ContainerArchive
    assert messages[0].archiving is True
    assert messages[0].msg.startswith("\nSaving image")
    assert [message.msg for message in messages[1:]] == [
        ".",
        "\033[A\r[============                                      ] 25%\n",
        "temporary warning\n",
    ]
