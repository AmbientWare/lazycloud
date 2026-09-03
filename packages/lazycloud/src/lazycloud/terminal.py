from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import TypeAlias

from typing_extensions import Self

ProgressCallback: TypeAlias = Callable[[int], None]


@dataclass
class TerminalStep:
    """One phase of a workflow: a name, a one-line summary, and the lines under it.

    ``update`` replaces the summary while the phase runs, ``log`` adds a line of
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
        self.summary = summary

    def progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.update(f"{min(100, completed * 100 // total)}%")

    def log(self, line: str) -> None:
        self.terminal.detail(line)

    def done(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self.terminal.line(f"   {self.name}: {self.summary} ({format_elapsed(self.elapsed)})")

    def fail(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self.terminal.error(f"{self.name} failed: {self.summary}")


@dataclass
class Terminal:
    quiet: bool = False

    def write(self, message: str) -> None:
        if not self.quiet:
            sys.stdout.write(message)
            sys.stdout.flush()

    def line(self, message: str = "") -> None:
        self.write(f"{message}\n")

    def error(self, message: str) -> None:
        if not self.quiet:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()

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
