from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from foundation.io_utils import OutputMessage

from images.building import ImageBuildStreamEventPlan


class ImageBuildLogStreamStatus(StrEnum):
    Pending = "pending"
    Running = "running"
    Stopping = "stopping"
    Complete = "complete"
    Error = "error"


class ImageBuildLogStreamClient(Protocol):
    def stream_logs(
        self,
        container_id: str,
        output: Callable[[OutputMessage], None],
    ) -> None: ...


@dataclass(slots=True)
class ImageBuildLogStreamDrain:
    events: list[ImageBuildStreamEventPlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: ImageBuildLogStreamStatus = ImageBuildLogStreamStatus.Pending


type ImageBuildLogEventMapper = Callable[[OutputMessage], ImageBuildStreamEventPlan]


@dataclass(slots=True)
class ImageBuildLogStreamCollector:
    client: ImageBuildLogStreamClient
    container_id: str
    event_mapper: ImageBuildLogEventMapper
    _events: list[ImageBuildStreamEventPlan] = field(default_factory=list, init=False)
    _errors: list[str] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop_requested: threading.Event = field(default_factory=threading.Event, init=False)
    _incomplete_shutdown_reported: bool = field(default=False, init=False)
    _status: ImageBuildLogStreamStatus = field(
        default=ImageBuildLogStreamStatus.Pending,
        init=False,
    )

    def start(self) -> ImageBuildLogStreamCollector:
        with self._lock:
            if self._thread is not None:
                return self
            if self._stop_requested.is_set():
                self._status = ImageBuildLogStreamStatus.Complete
                return self
            self._status = ImageBuildLogStreamStatus.Running
            self._thread = threading.Thread(
                target=self._run,
                name=f"image-build-logs-{self.container_id}",
                daemon=True,
            )
            self._thread.start()
        return self

    def join(self, timeout_seconds: float = 0.0) -> ImageBuildLogStreamStatus:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(timeout_seconds, 0.0))
        with self._lock:
            return self._status

    def close(self, timeout_seconds: float) -> ImageBuildLogStreamStatus:
        self._stop_requested.set()
        with self._lock:
            thread = self._thread
            if thread is None:
                self._status = ImageBuildLogStreamStatus.Complete
                return self._status
            if self._status is ImageBuildLogStreamStatus.Running:
                self._status = ImageBuildLogStreamStatus.Stopping

        thread.join(timeout=max(timeout_seconds, 0.0))
        with self._lock:
            if thread.is_alive():
                self._status = ImageBuildLogStreamStatus.Error
                if not self._incomplete_shutdown_reported:
                    self._errors.append(
                        "build container log stream did not stop before the shutdown deadline"
                    )
                    self._incomplete_shutdown_reported = True
            elif self._status is ImageBuildLogStreamStatus.Stopping:
                self._status = ImageBuildLogStreamStatus.Complete
            return self._status

    def drain(self) -> ImageBuildLogStreamDrain:
        with self._lock:
            events = list(self._events)
            errors = list(self._errors)
            status = self._status
            self._events.clear()
            self._errors.clear()
        return ImageBuildLogStreamDrain(events=events, errors=errors, status=status)

    def _run(self) -> None:
        try:
            self.client.stream_logs(self.container_id, self._capture)
        except Exception as exc:
            with self._lock:
                self._errors.append(str(exc) or exc.__class__.__name__)
                self._status = ImageBuildLogStreamStatus.Error
            return
        with self._lock:
            if self._status in {
                ImageBuildLogStreamStatus.Running,
                ImageBuildLogStreamStatus.Stopping,
            }:
                self._status = ImageBuildLogStreamStatus.Complete

    def _capture(self, message: OutputMessage) -> None:
        if self._stop_requested.is_set():
            return
        if not message.msg.strip():
            return
        event = self.event_mapper(message)
        with self._lock:
            if self._stop_requested.is_set():
                return
            self._events.append(event)
