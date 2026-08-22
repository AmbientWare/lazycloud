from __future__ import annotations

import io
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, TextIO

from pydantic import JsonValue
from shared.http.gateway_tasks import AppendTaskLogRequest, AppendTaskLogResponse

DEFAULT_GATEWAY_ENDPOINT = "http://127.0.0.1:9000"
DEFAULT_RUNNER_TIMEOUT_SECONDS = 30.0


class TaskLogControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue: ...


def post_task_log(
    control: TaskLogControlChannel,
    task_id: str,
    stream: str,
    message: str,
) -> None:
    if not message:
        return
    AppendTaskLogResponse.model_validate(
        control.post(
            "/gateway/tasks/log",
            AppendTaskLogRequest(task_id=task_id, stream=stream, message=message).model_dump(
                mode="json"
            ),
        )
    )


class RunnerTaskLogStream(io.TextIOBase):
    def __init__(self, stream: str, wrapped: TextIO) -> None:
        self.stream = stream
        self.wrapped = wrapped
        self._pending = ""
        # A handler is free to hand this stream to threads of its own, and two
        # of them appending to one buffer interleave into a line that belongs to
        # neither. The lock is over the buffer, not over the write to the real
        # stream, which is already serialized by the file object.
        self._lock = threading.Lock()
        self.dropped_appends = 0
        self.last_append_error = ""

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        self.wrapped.write(value)
        self.wrapped.flush()
        with self._lock:
            self._pending += value
            complete = self._take_complete_lines()
        for line in complete:
            self._append(line)
        return len(value)

    def flush(self) -> None:
        self.wrapped.flush()
        self.flush_log()

    def flush_log(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, ""
        if pending:
            self._append(pending)

    def append_log(self, value: str) -> None:
        raise NotImplementedError

    def _take_complete_lines(self) -> list[str]:
        lines: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", maxsplit=1)
            lines.append(f"{line}\n")
        return lines

    def _append(self, value: str) -> None:
        try:
            self.append_log(value)
        except Exception as exc:
            # `write` already put this line on the real stream, so only the
            # platform's copy is lost. Reporting through a logger would write
            # back into this same stream.
            self.dropped_appends += 1
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
    "install_context_routed_output",
    "required_env",
    "routed_output",
]
