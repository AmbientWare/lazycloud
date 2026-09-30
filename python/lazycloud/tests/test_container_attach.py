from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

from lazycloud.clients.gateway.control import GatewayControlClient
from shared.http.gateway import AttachToContainerResponse


def test_gateway_control_client_streams_attach_events() -> None:
    class FakeChannel:
        paths: list[str]

        def request_bytes(
            self,
            method: str,
            path: str,
            *,
            data: bytes | Iterable[bytes],
            headers: Mapping[str, str],
            timeout_seconds: float | None = None,
        ) -> bytes:
            raise AssertionError("attach does not send binary requests")

        def __init__(self) -> None:
            self.paths = []

        def stream_get(self, path: str) -> Iterator[str]:
            self.paths.append(path)
            yield ": connected\n"
            yield "\n"
            yield "id: ctr-1\n"
            yield "event: output\n"
            yield 'data: {"output": "ready\\n", "done": false}\n'
            yield "\n"
            yield "id: ctr-1\n"
            yield "event: done\n"
            yield 'data: {"output": "", "done": true, "exit_code": 0}\n'
            yield "\n"

        def get(self, path: str) -> object:
            raise AssertionError(path)

        def post(
            self,
            path: str,
            payload: dict[str, object] | None = None,
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            raise AssertionError(path)

    channel = FakeChannel()

    events = list(
        GatewayControlClient(channel).attach_to_container_events(
            "ctr-1",
            poll_interval_seconds=0.5,
        )
    )

    assert events == [
        AttachToContainerResponse(output="ready\n", done=False),
        AttachToContainerResponse(output="", done=True, exit_code=0),
    ]
    assert channel.paths == [
        "/gateway/containers/attach/stream?container_id=ctr-1&poll_interval_seconds=0.5"
        "&workspace=default"
    ]
