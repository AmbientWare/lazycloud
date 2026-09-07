from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import TypeAlias

from typing_extensions import Self

ProgressCallback: TypeAlias = Callable[[int], None]
_output_enabled: ContextVar[bool | None] = ContextVar("lazycloud_output_enabled", default=None)


@contextmanager
def output(*, enabled: bool = True) -> Iterator[None]:
    """Override SDK progress and replayed logs in this context, including async calls.

    Output goes to stderr. This does not redirect user prints, print return values,
    suppress exceptions, or change stored logs.
    """
    token = _output_enabled.set(enabled)
    try:
        yield
    finally:
        _output_enabled.reset(token)


@dataclass
class TerminalStep:
    """One phase of a workflow: a name, a one-line summary, and the lines under it.

    ``update`` reports the summary while the phase runs, ``log`` adds a line of
    output beneath it, and ``done``/``fail`` close it with the summary that
    stays on screen. Leaving the context with an exception fails the step.
    """

    name: str
    terminal: Terminal
    summary: str = ""
    started: float = field(default_factory=time.monotonic)
    finished: bool = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.finished:
            return
        if exc is None:
            self.done(self.summary)
        else:
            self.fail(self.summary)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def update(self, summary: str) -> None:
        if summary != self.summary:
            self.terminal.detail(f"{self.name}: {summary}")
        self.summary = summary

    def progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.update(f"{min(100, completed * 100 // total)}%")

    def log(self, line: str) -> None:
        self.terminal.detail(line)

    def done(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self.terminal.flush_remote_output()
        self.terminal.line(f"   {self.name}: {self.summary} ({format_elapsed(self.elapsed)})")

    def fail(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self.terminal.flush_remote_output()
        self.terminal.error(f"{self.name} failed: {self.summary}")


@dataclass
class Terminal:
    quiet: bool = False
    default_enabled: bool = True
    _remote_partial: bool = field(default=False, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        if self.quiet:
            return False
        override = _output_enabled.get()
        if override is not None:
            return override
        return self.default_enabled

    def write(self, message: str) -> None:
        if self.enabled:
            sys.stderr.write(message)
            sys.stderr.flush()

    def line(self, message: str = "") -> None:
        self.flush_remote_output()
        self.write(f"{message}\n")

    def remote_output(self, message: str, *, stream: str = "stdout") -> None:
        del stream
        if self.enabled and message:
            self.write(message)
            self._remote_partial = not message.endswith("\n")

    def flush_remote_output(self) -> None:
        if self._remote_partial:
            self.write("\n")
            self._remote_partial = False

    def error(self, message: str) -> None:
        self.line(message)

    def header(self, message: str) -> None:
        self.line(f"=> {message}")

    def detail(self, message: str) -> None:
        self.line(f"   {message}")

    def warn(self, message: str) -> None:
        self.line(f"WARNING: {message}")

    def success(self, message: str) -> None:
        self.line(message)

    def step(self, name: str, summary: str = "") -> TerminalStep:
        self.header(f"{name} {summary}".rstrip())
        return TerminalStep(name=name, terminal=self, summary=summary)


def format_elapsed(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, remaining = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remaining:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def humanize_bytes(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1000
    return f"{int(size)} B"
