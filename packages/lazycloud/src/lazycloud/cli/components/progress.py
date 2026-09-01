"""CLI progress backed by the shared output streams and semantic theme."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.style import Style

from lazycloud.cli.components import output, theme
from lazycloud.terminal import ProgressCallback, Terminal

_BUILD_PREFIXES = (
    "archive progress:",
    "building image",
    "cache key:",
    "collecting ",
    "copying ",
    "downloading ",
    "getting ",
    "installing ",
    "manifest written",
    "processing ",
    "resolved ",
    "step ",
    "successfully built",
    "successfully installed",
    "trying to pull",
    "writing manifest",
)


@dataclass
class CliTerminal(Terminal):
    _pending: str = field(default="", init=False)

    def write(self, message: str) -> None:
        if self.quiet or not message:
            return
        self._pending += message.replace("\r", "\n")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", maxsplit=1)
            self._print_line(line)

    def line(self, message: str = "") -> None:
        if self.quiet:
            return
        self._flush_pending()
        self._print_line(message)

    def error(self, message: str) -> None:
        if self.quiet:
            return
        self._flush_pending()
        output.error_console.print(theme.styled(message, theme.ERROR + theme.EMPHASIS))

    def header(self, message: str) -> None:
        self.line(message)

    def detail(self, message: str) -> None:
        self.line(message)

    def warn(self, message: str) -> None:
        if self.quiet:
            return
        self._flush_pending()
        output.console.print(theme.styled(message, theme.WARNING))

    def success(self, message: str) -> None:
        if self.quiet:
            return
        self._flush_pending()
        output.console.print(theme.styled(message, theme.SUCCESS + theme.EMPHASIS))

    @contextmanager
    def progress_bytes(self, description: str, *, total: int) -> Iterator[ProgressCallback]:
        if self.quiet or total <= 0:
            yield lambda _completed: None
            return
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=output.console,
            transient=False,
        )
        with progress:
            task_id = progress.add_task(description, total=total)

            def update(completed: int) -> None:
                progress.update(task_id, completed=max(0, min(completed, total)))

            yield update

    def _flush_pending(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self._print_line(pending)

    def _print_line(self, message: str) -> None:
        if not message:
            output.console.print()
            return
        output.console.print(theme.styled(message, output_style(message)))


@runtime_checkable
class TerminalAware(Protocol):
    terminal: Terminal | None


@runtime_checkable
class TerminalResourceCollection(Protocol):
    @property
    def resources(self) -> tuple[object, ...]: ...


def attach_terminal(target: object, terminal: CliTerminal | None = None) -> CliTerminal:
    selected = terminal or CliTerminal()
    if isinstance(target, TerminalAware):
        target.terminal = selected
    if isinstance(target, TerminalResourceCollection):
        for resource in target.resources:
            attach_terminal(resource, selected)
    return selected


def output_style(message: str) -> Style:
    text = message.strip()
    lower = text.lower()
    if not text:
        return theme.MUTED
    if lower.startswith("warning:") or lower.startswith("[notice]"):
        return theme.WARNING
    if "traceback " in lower or " error" in lower or lower.startswith("error"):
        return theme.ERROR
    if lower.startswith("preparing "):
        return theme.RUNNING
    if lower.startswith("submitted task"):
        return theme.PENDING
    if lower.startswith("task <"):
        if "complete" in lower:
            return theme.SUCCESS
        if "failed" in lower or "cancel" in lower or "timeout" in lower:
            return theme.ERROR
        return theme.PENDING
    if lower.startswith(("function complete", "build complete", "build completed successfully")):
        return theme.SUCCESS
    if lower.startswith(_BUILD_PREFIXES) or lower.startswith(("-->", "+ ")):
        return theme.INFO
    return theme.PLAIN


def print_stream_message(stream: str, message: str) -> None:
    output.write_stream(
        message if message.endswith("\n") else f"{message}\n",
        error=stream == "stderr",
    )


__all__ = ["CliTerminal", "attach_terminal", "output_style", "print_stream_message"]
