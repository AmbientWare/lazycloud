from __future__ import annotations

import io
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from queue import Empty, Queue
from typing import Protocol, TextIO

from pydantic import JsonValue
from shared.http.gateway_tasks import AppendTaskLogRequest, AppendTaskLogResponse

DEFAULT_GATEWAY_ENDPOINT = "http://127.0.0.1:9000"
DEFAULT_RUNNER_TIMEOUT_SECONDS = 30.0


class TaskLogControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue: ...


def post_task_logs(
    control: TaskLogControlChannel,
    task_id: str,
    stream: str,
    messages: str | list[str],
) -> None:
    if not messages:
        return
    AppendTaskLogResponse.model_validate(
        control.post(
            "/gateway/tasks/log",
            AppendTaskLogRequest(task_id=task_id, stream=stream, message=messages).model_dump(
                mode="json"
            ),
        )
    )


class RunnerTaskLogStream(io.TextIOBase):
    def __init__(self, stream: str, wrapped: TextIO, logs: TaskLogBuffer) -> None:
        self.stream = stream
        self.wrapped = wrapped
        self.logs = logs
        self._pending = ""
        self._lock = threading.Lock()
        self._closing = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        with self._lock:
            if self._closing:
                raise ValueError("I/O operation on closed task log stream")
            self.wrapped.write(value)
            self.wrapped.flush()
            self._pending += value
            lines = self._pending.split("\n")
            self._pending = lines.pop()
            for line in lines:
                self.logs.append(self.stream, line + "\n")
        return len(value)

    def flush(self) -> None:
        self.wrapped.flush()
        with self._lock:
            pending, self._pending = self._pending, ""
            if pending and not self._closing:
                self.logs.append(self.stream, pending)

    def close(self) -> None:
        with self._lock:
            if not self._closing:
                if self._pending:
                    self.logs.append(self.stream, self._pending)
                    self._pending = ""
                self._closing = True
        super().close()


class TaskLogBuffer:
    def __init__(self, append_logs: Callable[[str, list[str]], None]) -> None:
        self._append_logs = append_logs
        self._queue: Queue[tuple[str, str] | None] = Queue(maxsize=128)
        self._lock = threading.Lock()
        self._sender: threading.Thread | None = None
        self._closing = False
        self.dropped_appends = 0
        self.last_append_error = ""

    def append(self, stream: str, value: str) -> None:
        with self._lock:
            if self._closing:
                raise ValueError("Task log buffer is closed")
            if self._sender is None:
                self._sender = threading.Thread(target=self._send, daemon=True)
                self._sender.start()
            self._queue.put((stream, value))

    def close(self) -> None:
        with self._lock:
            if not self._closing:
                self._closing = True
                if self._sender is not None:
                    self._queue.put(None)
        if self._sender is not None:
            self._sender.join()

    def _send(self) -> None:
        pending: tuple[str, str] | None = None
        while True:
            first = pending if pending is not None else self._queue.get()
            pending = None
            if first is None:
                return
            stream, message = first
            batch = [message]
            size = len(message)
            deadline = time.monotonic() + 0.05
            closing = False
            while len(batch) < 64 and size < 16_384:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    value = self._queue.get(timeout=remaining)
                except Empty:
                    break
                if value is None:
                    closing = True
                    break
                if value[0] != stream:
                    pending = value
                    break
                batch.append(value[1])
                size += len(value[1])
            self._append(stream, batch)
            if closing:
                return

    def _append(self, stream: str, values: list[str]) -> None:
        try:
            self._append_logs(stream, values)
        except Exception as exc:
            # `write` already put this line on the real stream, so only the
            # platform's copy is lost. Reporting through a logger would write
            # back into this same stream.
            self.dropped_appends += len(values)
            self.last_append_error = f"{type(exc).__name__}: {exc}"


class RoutedSink(Protocol):
    """Where output written inside one invocation's context goes.

    Structural because the two things that stand in front of a stream here are
    unrelated: a task's log stream, and the buffer a lifecycle hook reports
    through. Both are only ever written to and flushed.
    """

    def write(self, value: str, /) -> int: ...

    def flush(self) -> None: ...


_STDOUT_SINK: ContextVar[RoutedSink | None] = ContextVar("runner_stdout_sink", default=None)
_STDERR_SINK: ContextVar[RoutedSink | None] = ContextVar("runner_stderr_sink", default=None)


class _ContextRoutedStream(io.TextIOBase):
    """Sends each write to whichever task the writing context belongs to.

    `contextlib.redirect_stdout` cannot serve concurrent invocations. It swaps
    one process-wide `sys.stdout` and restores what it found, so two overlapping
    redirects restore in the order they exit rather than the order they entered:
    the inner one puts back the outer one's stream, and the outer one then puts
    back a stream belonging to a task that has finished — permanently, for every
    later caller.

    Installed once and never swapped. The routing is a context lookup, so a
    handler's output reaches its own task's log whether the concurrent slots are
    threads or coroutines, and output from no task at all — startup, `on_start`,
    the claim loop — falls through to the container's own stream.
    """

    def __init__(
        self,
        wrapped: TextIO,
        holder: ContextVar[RoutedSink | None],
    ) -> None:
        self.underlying = wrapped
        self._holder = holder

    @property
    def _target(self) -> RoutedSink | TextIO:
        return self._holder.get() or self.underlying

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, value: str) -> int:
        return self._target.write(value)

    def flush(self) -> None:
        self._target.flush()

    def fileno(self) -> int:
        return self.underlying.fileno()


def install_context_routed_output() -> tuple[TextIO, TextIO]:
    """Point `sys.stdout`/`sys.stderr` at the context router, once.

    Returns the real streams. Per-task sinks must wrap these rather than
    whatever `sys.stdout` currently is, or a sink installed while the router is
    active writes back into the router and recurses.
    """

    stdout = _underlying(sys.stdout)
    stderr = _underlying(sys.stderr)
    sys.stdout = _ContextRoutedStream(stdout, _STDOUT_SINK)
    sys.stderr = _ContextRoutedStream(stderr, _STDERR_SINK)
    return stdout, stderr


def _underlying(stream: TextIO) -> TextIO:
    if isinstance(stream, _ContextRoutedStream):
        return stream.underlying
    return stream


@contextmanager
def routed_output(stdout: RoutedSink | None, stderr: RoutedSink | None) -> Iterator[None]:
    """Send output written in this context to the given sinks."""

    stdout_token = _STDOUT_SINK.set(stdout)
    stderr_token = _STDERR_SINK.set(stderr)
    try:
        yield
    finally:
        _STDERR_SINK.reset(stderr_token)
        _STDOUT_SINK.reset(stdout_token)


def required_env(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        msg = f"{key} is required"
        raise RuntimeError(msg)
    return value


__all__ = [
    "DEFAULT_GATEWAY_ENDPOINT",
    "DEFAULT_RUNNER_TIMEOUT_SECONDS",
    "RoutedSink",
    "RunnerTaskLogStream",
    "TaskLogBuffer",
    "install_context_routed_output",
    "required_env",
    "routed_output",
]
