"""CLI progress: one live step at a time, backed by the shared output streams."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rich.console import Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.style import Style
from rich.table import Table
from rich.text import Text

from lazycloud.cli.components import output, theme
from lazycloud.cli.components.errors import debug_errors_enabled
from lazycloud.terminal import Terminal, TerminalStep, format_elapsed

TAIL_LINES = 3
FAILURE_TAIL_LINES = 20
NAME_WIDTH = 11
_LOG_INDENT = "    "


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

    def step(self, name: str, summary: str = "") -> TerminalStep:
        self._flush_pending()
        return LiveStep(name=name, terminal=self, summary=summary)

    def _flush_pending(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self._print_line(pending)

    def _print_line(self, message: str) -> None:
        if not message:
            output.console.print()
            return
        output.console.print(theme.styled(message, output_style(message)))


@dataclass
class LiveStep(TerminalStep):
    """A step drawn as a spinner line with a short tail of its output.

    While the step runs, the line and its tail redraw in place at the bottom of
    the screen; anything else printed lands above them. Finishing replaces the
    live line with a permanent one. Without a terminal, or with debug errors on,
    every logged line prints as it arrives instead of scrolling through the tail.
    """

    _tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES), init=False)
    _recent: deque[str] = field(
        default_factory=lambda: deque(maxlen=FAILURE_TAIL_LINES), init=False
    )
    _live: Live | None = field(default=None, init=False)
    _spinner: Spinner = field(default_factory=lambda: Spinner("dots", style=theme.RUNNING))

    def __post_init__(self) -> None:
        if self.terminal.quiet:
            return
        if _interactive():
            self._live = Live(
                self._render_live(),
                console=output.console,
                refresh_per_second=12,
                transient=True,
            )
            self._live.start()

    def update(self, summary: str) -> None:
        super().update(summary)
        self._refresh()

    def progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.update(
                f"{self.summary.split('·', 1)[0].strip()} · {min(100, completed * 100 // total)}%"
            )

    def log(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        self._recent.append(text)
        if self._verbose_logs():
            output.console.print(Text(f"{_LOG_INDENT}{text}", style=theme.MUTED))
            return
        self._tail.append(text)
        self._refresh()

    def done(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self._stop()
        self._print_final("✓", theme.SUCCESS)

    def fail(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self._stop()
        if not self._verbose_logs():
            for text in self._recent:
                output.console.print(Text(f"{_LOG_INDENT}{text}", style=theme.MUTED))
        self._print_final("✗", theme.ERROR)

    def _verbose_logs(self) -> bool:
        return self._live is None or debug_errors_enabled()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render_live())

    def _stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _print_final(self, glyph: str, style: Style) -> None:
        if self.terminal.quiet:
            return
        output.console.print(
            _step_row(Text(glyph, style=style), self.name, self.summary, self.elapsed)
        )

    def _render_live(self) -> RenderableType:
        row = _step_row(self._spinner, self.name, self.summary, self.elapsed)
        if not self._tail:
            return row
        tail = [
            Text(f"{_LOG_INDENT}{text}", style=theme.MUTED, no_wrap=True) for text in self._tail
        ]
        return Group(row, *tail)


def _interactive() -> bool:
    """Live redraws need a real terminal on the stream the console writes to.

    FORCE_COLOR makes Rich report a terminal even for a pipe, and JSON output
    keeps the human stream on stderr, so neither can stand in for a tty check.
    """
    if output.json_output_active():
        return False
    stream = output.console.file
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _step_row(glyph: RenderableType, name: str, summary: str, elapsed: float) -> Table:
    grid = Table.grid(padding=(0, 1), expand=False)
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(width=NAME_WIDTH, no_wrap=True)
    grid.add_column(min_width=32, max_width=56, no_wrap=True)
    grid.add_column(justify="right", width=7, no_wrap=True)
    grid.add_row(
        glyph,
        Text(name, style=theme.EMPHASIS),
        Text(summary),
        Text(format_elapsed(elapsed), style=theme.MUTED),
    )
    return grid


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
    return theme.PLAIN


def print_stream_message(stream: str, message: str) -> None:
    output.write_stream(
        message if message.endswith("\n") else f"{message}\n",
        error=stream == "stderr",
    )


__all__ = ["CliTerminal", "LiveStep", "attach_terminal", "output_style", "print_stream_message"]
