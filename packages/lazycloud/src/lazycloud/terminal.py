from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from types import TracebackType
from typing import TypeAlias

from rich.console import Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.style import Style
from rich.table import Table
from rich.text import Text
from shared.http.task_progress import TaskPendingProgress, TaskPendingReason
from shared.timestamps import utc_now
from typing_extensions import Self

from lazycloud._terminal import theme
from lazycloud._terminal.cards import notice_card
from lazycloud._terminal.streams import error_console, json_output_active

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


NAME_WIDTH = 11
TAIL_LINES = 3
FAILURE_TAIL_LINES = 20
_LOG_INDENT = "    "
_REMOTE_RAIL = "│ "


@dataclass
class TerminalStep:
    """A workflow phase with a log preview and a retained failure tail."""

    name: str
    terminal: Terminal
    summary: str = ""
    started: float = field(default_factory=time.monotonic)
    finished: bool = False

    _recent: deque[str] = field(
        default_factory=lambda: deque(maxlen=FAILURE_TAIL_LINES), init=False
    )
    _spinner: Spinner = field(default_factory=lambda: Spinner("dots", style=theme.RUNNING))
    _pending_notice: tuple[str, TaskPendingProgress] | None = field(default=None, init=False)

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

    def __post_init__(self) -> None:
        _display.start(self)

    def update(self, summary: str) -> None:
        with _display.lock:
            self.summary = summary
            _display.refresh()

    def progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.update(
                f"{self.summary.split('·', 1)[0].strip()} · {min(100, completed * 100 // total)}%"
            )

    def pending_progress(self, task_id: str, pending: TaskPendingProgress | None) -> None:
        with _display.lock:
            self._pending_notice = (task_id, pending) if pending is not None else None
            if _display.live is None:
                self.terminal.pending_progress(task_id, pending)
            else:
                _display.refresh()

    def log(self, line: str) -> None:
        text = line.rstrip()
        if not self.terminal.enabled or not text:
            return
        with _display.lock:
            self._recent.append(text)
            if _display.live is None or self.terminal.verbose:
                error_console.print(Text(f"{_LOG_INDENT}{text}", style=theme.MUTED))
            else:
                _display.refresh()

    def done(self, summary: str = "") -> None:
        with _display.lock:
            self.finished = True
            self.summary = summary or self.summary
            _display.stop(self)
            self._print_final("✓", theme.SUCCESS)

    def dismiss(self) -> None:
        """Finish without leaving a line, for a wait whose end the next output shows."""
        with _display.lock:
            self.finished = True
            _display.stop(self)

    def fail(self, summary: str = "") -> None:
        with _display.lock:
            self.finished = True
            self.summary = summary or self.summary
            show_tail = _display.live is not None and not self.terminal.verbose
            _display.stop(self)
            if self.terminal.enabled and show_tail:
                for text in self._recent:
                    error_console.print(Text(f"{_LOG_INDENT}{text}", style=theme.MUTED))
            self._print_final("✗", theme.ERROR)

    def _print_final(self, glyph: str, style: Style) -> None:
        if not self.terminal.enabled:
            return
        error_console.print(
            _step_row(Text(glyph, style=style), self.name, self.summary, self.elapsed)
        )

    def _render_live(self) -> RenderableType:
        row = _step_row(self._spinner, self.name, self.summary, self.elapsed)
        parts: list[RenderableType] = [row]
        pending_notice = self._pending_notice
        if pending_notice is not None:
            parts.append(_pending_card(*pending_notice))
        if not self.terminal.verbose:
            parts.extend(
                Text(f"{_LOG_INDENT}{text}", style=theme.MUTED, no_wrap=True)
                for text in list(self._recent)[-TAIL_LINES:]
            )
        return Group(*parts)


@dataclass
class _ProgressDisplay:
    """One live display across concurrent SDK calls and CLI resources."""

    steps: list[TerminalStep] = field(default_factory=list)
    live: Live | None = None
    lock: RLock = field(default_factory=RLock)

    def start(self, step: TerminalStep) -> None:
        with self.lock:
            if not step.terminal.enabled:
                return
            self.steps.append(step)
            if self.live is None and _interactive():
                self.live = Live(
                    get_renderable=self.render,
                    console=error_console,
                    refresh_per_second=12,
                    transient=True,
                    redirect_stdout=False,
                    redirect_stderr=False,
                )
                self.live.start(refresh=True)
            self.refresh()

    def stop(self, step: TerminalStep) -> None:
        with self.lock:
            if not any(active is step for active in self.steps):
                return
            self.steps = [active for active in self.steps if active is not step]
            if self.steps:
                self.refresh()
            elif self.live is not None:
                self.live.stop()
                self.live = None
            step.terminal.flush_remote_output()

    def refresh(self) -> None:
        if self.live is not None:
            self.live.refresh()

    def render(self) -> RenderableType:
        # The refresh thread holds Live's lock. Snapshot steps without taking
        # our lock, which callers hold while refreshing or stopping Live.
        return Group(*(step._render_live() for step in tuple(self.steps)))


_display = _ProgressDisplay()


@dataclass
class Terminal:
    quiet: bool = False
    default_enabled: bool = True
    verbose: bool = False
    _pending: str = field(default="", init=False)
    _remote_pending: str = field(default="", init=False)
    _remote_stream: str = field(default="stdout", init=False)

    @property
    def enabled(self) -> bool:
        if self.quiet:
            return False
        override = _output_enabled.get()
        if override is not None:
            return override
        return self.default_enabled

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
        error_console.print(theme.styled(message, theme.ERROR + theme.EMPHASIS))

    def header(self, message: str) -> None:
        self.line(message)

    def detail(self, message: str) -> None:
        self.line(message)

    def pending_progress(self, task_id: str, pending: TaskPendingProgress | None) -> None:
        if self.enabled and pending is not None:
            self.flush_remote_output()
            self._flush_pending()
            error_console.print(_pending_card(task_id, pending))

    def warn(self, message: str) -> None:
        if not self.enabled:
            return
        self.flush_remote_output()
        self._flush_pending()
        error_console.print(theme.styled(message, theme.WARNING))

    def success(self, message: str) -> None:
        if not self.enabled:
            return
        self.flush_remote_output()
        self._flush_pending()
        error_console.print(theme.styled(message, theme.SUCCESS + theme.EMPHASIS))

    def step(self, name: str, summary: str = "") -> TerminalStep:
        with _display.lock:
            if self.enabled:
                self.flush_remote_output()
                self._flush_pending()
            return TerminalStep(name=name, terminal=self, summary=summary)

    def _flush_pending(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self._print_line(pending)

    def _print_line(self, message: str) -> None:
        if not self.enabled:
            return
        if not message:
            error_console.print()
            return
        error_console.print(theme.styled(message, _output_style(message)))

    def _print_remote_line(self, message: str, *, stream: str) -> None:
        if not self.enabled:
            return
        rail_style = theme.ERROR if stream == "stderr" else theme.RUNNING
        message_style = theme.ERROR if stream == "stderr" else theme.MUTED
        line = Text()
        line.append(_REMOTE_RAIL, style=rail_style)
        line.append(message, style=message_style)
        error_console.print(line)


def _pending_card(task_id: str, pending: TaskPendingProgress) -> RenderableType:
    elapsed = format_elapsed((utc_now() - pending.pending_since).total_seconds())
    hint = {
        TaskPendingReason.Queued: "Keep this command open to follow execution.",
        TaskPendingReason.Dependencies: "Check the input tasks if they are not progressing.",
        TaskPendingReason.Retry: "The next attempt will start automatically.",
        TaskPendingReason.CapacityBusy: "Queued work can start when a container has room.",
        TaskPendingReason.CapacityUnavailable: (
            "Placement will retry automatically. Check compute status for available capacity."
        ),
        TaskPendingReason.CapacityLimit: "Check compute policy and the configured capacity limit.",
        TaskPendingReason.ProvisioningCompute: "Keep this command open to follow compute startup.",
        TaskPendingReason.StartingContainer: "Check container logs if startup stops progressing.",
    }[pending.reason]
    return notice_card(
        pending.message,
        title=f"Task {task_id[:8]} · pending {elapsed}",
        hint=hint,
        tone="warning"
        if pending.reason
        in {TaskPendingReason.CapacityUnavailable, TaskPendingReason.CapacityLimit}
        else "info",
    )


def _interactive() -> bool:
    """Live redraws need a real terminal on the stream the console writes to.

    FORCE_COLOR makes Rich report a terminal even for a pipe, and JSON output
    keeps the human stream on stderr, so neither can stand in for a tty check.
    """
    if json_output_active() or error_console.is_dumb_terminal:
        return False
    stream = error_console.file
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _step_row(glyph: RenderableType, name: str, summary: str, elapsed: float) -> Table:
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(width=NAME_WIDTH, no_wrap=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", width=7, no_wrap=True)
    grid.add_row(
        glyph,
        Text(name, style=theme.EMPHASIS),
        Text(summary),
        Text(format_elapsed(elapsed), style=theme.MUTED),
    )
    return grid


def _output_style(message: str) -> Style:
    text = message.strip()
    lower = text.lower()
    if not text:
        return theme.MUTED
    if lower.startswith("warning:") or lower.startswith("[notice]"):
        return theme.WARNING
    if "traceback " in lower or " error" in lower or lower.startswith("error"):
        return theme.ERROR
    return theme.PLAIN


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
