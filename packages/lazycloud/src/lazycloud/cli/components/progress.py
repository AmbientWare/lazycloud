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

NAME_WIDTH = 11
TAIL_LINES = 3
FAILURE_TAIL_LINES = 20
_LOG_INDENT = "    "
_REMOTE_RAIL = "│ "


@dataclass
class CliTerminal(Terminal):
    _pending: str = field(default="", init=False)
    _remote_pending: str = field(default="", init=False)
    _remote_stream: str = field(default="stdout", init=False)

    def write(self, message: str) -> None:
        if not self.enabled or not message:
            return
        self.flush_remote_output()
        self._pending += message.replace("\r", "\n")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", maxsplit=1)
            self._print_line(line)

    def line(self, message: str = "") -> None:
        if not self.enabled:
            return
        self.flush_remote_output()
        self._flush_pending()
        self._print_line(message)

    def remote_output(self, message: str, *, stream: str = "stdout") -> None:
        if not self.enabled or not message:
            return
        self._flush_pending()
        if self._remote_pending and stream != self._remote_stream:
            self.flush_remote_output()
        self._remote_stream = stream
        self._remote_pending += message.replace("\r", "\n")
        while "\n" in self._remote_pending:
            line, self._remote_pending = self._remote_pending.split("\n", maxsplit=1)
            self._print_remote_line(line, stream=stream)

    def flush_remote_output(self) -> None:
        if self._remote_pending:
            pending, self._remote_pending = self._remote_pending, ""
            self._print_remote_line(pending, stream=self._remote_stream)

    def error(self, message: str) -> None:
        if not self.enabled:
            return
        self.flush_remote_output()
        self._flush_pending()
        output.error_console.print(theme.styled(message, theme.ERROR + theme.EMPHASIS))

    def header(self, message: str) -> None:
        self.line(message)

    def detail(self, message: str) -> None:
        self.line(message)

    def warn(self, message: str) -> None:
        if not self.enabled:
            return
        self.flush_remote_output()
        self._flush_pending()
        output.error_console.print(theme.styled(message, theme.WARNING))

    def success(self, message: str) -> None:
        if not self.enabled:
            return
        self.flush_remote_output()
        self._flush_pending()
        output.error_console.print(theme.styled(message, theme.SUCCESS + theme.EMPHASIS))

    def step(self, name: str, summary: str = "") -> TerminalStep:
        self.flush_remote_output()
        self._flush_pending()
        return LiveStep(name=name, terminal=self, summary=summary)

    def _flush_pending(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self._print_line(pending)

    def _print_line(self, message: str) -> None:
        if not self.enabled:
            return
        if not message:
            output.error_console.print()
            return
        output.error_console.print(theme.styled(message, output_style(message)))

    def _print_remote_line(self, message: str, *, stream: str) -> None:
        if not self.enabled:
            return
        rail_style = theme.ERROR if stream == "stderr" else theme.RUNNING
        message_style = theme.ERROR if stream == "stderr" else theme.MUTED
        line = Text()
        line.append(_REMOTE_RAIL, style=rail_style)
        line.append(message, style=message_style)
        output.error_console.print(line)


@dataclass
class LiveStep(TerminalStep):
    """A live status line with a rolling log preview and a retained failure tail."""

    _recent: deque[str] = field(
        default_factory=lambda: deque(maxlen=FAILURE_TAIL_LINES), init=False
    )
    _live: Live | None = field(default=None, init=False)
    _spinner: Spinner = field(default_factory=lambda: Spinner("dots", style=theme.RUNNING))

    def __post_init__(self) -> None:
        if not self.terminal.enabled:
            return
        if _interactive():
            self._live = Live(
                self._render_live(),
                console=output.error_console,
                refresh_per_second=12,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._live.start()

    def update(self, summary: str) -> None:
        self.summary = summary
        self._refresh()

    def progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.update(
                f"{self.summary.split('·', 1)[0].strip()} · {min(100, completed * 100 // total)}%"
            )

    def log(self, line: str) -> None:
        text = line.rstrip()
        if not self.terminal.enabled or not text:
            return
        self._recent.append(text)
        if self._live is None or debug_errors_enabled():
            output.error_console.print(Text(f"{_LOG_INDENT}{text}", style=theme.MUTED))
        else:
            self._refresh()

    def done(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        self._stop()
        self._print_final("✓", theme.SUCCESS)

    def fail(self, summary: str = "") -> None:
        self.finished = True
        self.summary = summary or self.summary
        show_tail = self._live is not None and not debug_errors_enabled()
        self._stop()
        if self.terminal.enabled and show_tail:
            for text in self._recent:
                output.error_console.print(Text(f"{_LOG_INDENT}{text}", style=theme.MUTED))
        self._print_final("✗", theme.ERROR)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render_live())

    def _stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self.terminal.flush_remote_output()

    def _print_final(self, glyph: str, style: Style) -> None:
        if not self.terminal.enabled:
            return
        output.error_console.print(
            _step_row(Text(glyph, style=style), self.name, self.summary, self.elapsed)
        )

    def _render_live(self) -> RenderableType:
        row = _step_row(self._spinner, self.name, self.summary, self.elapsed)
        if debug_errors_enabled() or not self._recent:
            return row
        return Group(
            row,
            *(
                Text(f"{_LOG_INDENT}{text}", style=theme.MUTED, no_wrap=True)
                for text in list(self._recent)[-TAIL_LINES:]
            ),
        )


def _interactive() -> bool:
    """Live redraws need a real terminal on the stream the console writes to.

    FORCE_COLOR makes Rich report a terminal even for a pipe, and JSON output
    keeps the human stream on stderr, so neither can stand in for a tty check.
    """
    if output.json_output_active():
        return False
    stream = output.error_console.file
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
