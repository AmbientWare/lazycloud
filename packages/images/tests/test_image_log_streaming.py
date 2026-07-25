from __future__ import annotations

import threading
from collections.abc import Callable

from foundation.io_utils import OutputMessage
from images.building import plan_image_build_log_event
from images.log_streaming import ImageBuildLogStreamCollector, ImageBuildLogStreamStatus


def test_image_build_log_stream_close_is_idempotent_after_completion() -> None:
    client = _CompletedLogClient()
    collector = ImageBuildLogStreamCollector(
        client,
        "container-1",
        lambda message: plan_image_build_log_event(message.msg),
    ).start()
    assert client.complete.wait(timeout=1)

    assert collector.close(0.1) is ImageBuildLogStreamStatus.Complete
    assert collector.close(0.1) is ImageBuildLogStreamStatus.Complete
    drained = collector.drain()

    assert [event.message for event in drained.events] == ["ready\n"]
    assert drained.errors == []


def test_image_build_log_stream_close_reports_incomplete_shutdown_once() -> None:
    client = _BlockedLogClient()
    collector = ImageBuildLogStreamCollector(
        client,
        "container-1",
        lambda message: plan_image_build_log_event(message.msg),
    ).start()
    assert client.started.wait(timeout=1)
    try:
        assert collector.close(0) is ImageBuildLogStreamStatus.Error
        assert collector.close(0) is ImageBuildLogStreamStatus.Error
        drained = collector.drain()

        assert drained.events == []
        assert drained.errors == [
            "build container log stream did not stop before the shutdown deadline"
        ]
    finally:
        client.release.set()
        assert client.complete.wait(timeout=1)


class _CompletedLogClient:
    def __init__(self) -> None:
        self.complete = threading.Event()

    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None:
        del container_id
        output(OutputMessage(msg="ready\n"))
        self.complete.set()


class _BlockedLogClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.complete = threading.Event()

    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None:
        del container_id, output
        self.started.set()
        self.release.wait()
        self.complete.set()
