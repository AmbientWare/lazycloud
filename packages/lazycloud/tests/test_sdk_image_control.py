from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lazycloud.clients.image.control import ImageControlClient
from shared.http.errors import HttpTransportError
from shared.http.images import BuildImageRequest


class _ReconnectingImageChannel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        raise AssertionError((path, payload))

    def stream_post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        self.calls.append((path, payload))
        running = {
            "build_id": "build-1",
            "msg": "building",
            "status": "running",
            "phase": "manifest",
        }
        yield running
        if len(self.calls) == 1:
            raise HttpTransportError("POST", "https://api.test/images/build", "disconnected")
        yield {
            "build_id": "build-1",
            "image_id": "image-1",
            "msg": "complete",
            "done": True,
            "success": True,
            "status": "complete",
            "phase": "complete",
        }


def test_image_build_stream_reconnects_without_repeating_progress() -> None:
    channel = _ReconnectingImageChannel()
    client = ImageControlClient(channel, workspace="workspace-1")

    responses = list(client.build_image(BuildImageRequest(python_packages=["httpx"])))

    assert [response.msg for response in responses] == ["building", "complete"]
    assert responses[-1].done
    assert responses[-1].success
    assert len(channel.calls) == 2
    assert channel.calls[0] == channel.calls[1]
    assert channel.calls[0][0].endswith("?workspace=workspace-1")
