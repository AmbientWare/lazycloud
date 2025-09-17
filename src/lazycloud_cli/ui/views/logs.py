from collections import deque
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

DEFAULT_MAX_LINES = 1000
DEFAULT_REFRESH_RATE = 10
PANEL_PADDING = 4
MIN_VISIBLE_LINES = 1
MOUSE_SCROLL_LINES = 3
PAGE_SCROLL_FACTOR = 0.33
MIN_PAGE_SCROLL = 5


class LogViewer:
    def __init__(
        self, console: Console, service_name: str, max_lines: int = DEFAULT_MAX_LINES
    ) -> None:
        self.console = console
        self.service_name = service_name
        self.max_lines = max_lines
        self.log_messages: deque[str] = deque(maxlen=max_lines)

        self._live: Optional[Live] = None
        self._follow = True
        self._error_message: Optional[str] = None
        self._view_top_index = 0
        self._auto_scroll = True
        self._paused = False

    def add_log_line(self, message: Optional[str]) -> None:
        if message is None or self._paused:
            return

        try:
            text = Text.from_ansi(message)
            cleaned = text.plain.rstrip()
        except Exception:
            cleaned = (
                message.rstrip() if isinstance(message, str) else str(message).rstrip()
            )

        if cleaned:  # Only add non-empty lines
            self.log_messages.append(cleaned)

        if self._auto_scroll and self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass  # Ignore render errors during shutdown

    def set_error(self, error: str) -> None:
        self._error_message = error
        if self._live:
            self._live.update(self._render())

    def _calculate_visible_lines(self) -> int:
        return max(MIN_VISIBLE_LINES, self.console.height - PANEL_PADDING)

    def _render(self) -> Panel:
        width = self.console.width
        height = self.console.height
        visible_lines = self._calculate_visible_lines()

        display_logs = self._get_visible_logs(visible_lines)

        while len(display_logs) < visible_lines:
            display_logs.append("")

        content = "\n".join(display_logs)

        return Panel(
            content,
            title=f"📋 Logs: {self.service_name}",
            title_align="left",
            border_style="bright_blue",
            subtitle=self._create_status_line(),
            subtitle_align="right",
            height=height,
            width=width,
        )

    def _get_visible_logs(self, visible_lines: int) -> list[str]:
        if not self.log_messages or visible_lines <= 0:
            return []

        total = len(self.log_messages)

        if self._auto_scroll:
            if total <= visible_lines:
                return list(self.log_messages)
            return list(self.log_messages)[-visible_lines:]

        self._view_top_index = max(0, min(self._view_top_index, total - 1))
        end_index = min(self._view_top_index + visible_lines, total)
        return list(self.log_messages)[self._view_top_index : end_index]

    def _create_status_line(self) -> str:
        current_time = datetime.now().strftime("%H:%M:%S")

        if self._error_message:
            return f"{self._error_message} • {current_time}"

        total_logs = len(self.log_messages)

        if total_logs == 0:
            position_info = "No logs"
        elif self._paused:
            position_info = "PAUSED"
        elif self._auto_scroll:
            position_info = "Auto-scrolling"
        else:
            position_info = f"Line {self._view_top_index + 1}/{total_logs}"

        key_hints = " • ↑↓ • PgUp/PgDn • a=auto-scroll • Space=pause"
        mode = "Following" if self._follow else "Showing"
        return f"{mode} logs ({total_logs} lines) • {position_info}{key_hints} • {current_time}"

    def start(self) -> Live:
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=DEFAULT_REFRESH_RATE,
            transient=False,
            screen=True,
        )
        return self._live

    def stop(self) -> None:
        self._follow = False
        if self._live:
            self._live.stop()
            self._live = None

    def scroll_up(self, lines: int = 1) -> None:
        if lines <= 0 or not self.log_messages:
            return

        if self._auto_scroll:
            visible_lines = self._calculate_visible_lines()
            total = len(self.log_messages)
            self._view_top_index = max(0, total - visible_lines)

        self._view_top_index = max(0, self._view_top_index - lines)
        self._auto_scroll = False

        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass

    def scroll_down(self, lines: int = 1) -> None:
        if lines <= 0 or not self.log_messages:
            return

        total = len(self.log_messages)
        visible_lines = self._calculate_visible_lines()
        max_index = max(0, total - visible_lines)

        self._view_top_index = min(max_index, self._view_top_index + lines)

        if self._view_top_index >= max_index:
            self._auto_scroll = True

        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass

    def page_up(self) -> None:
        visible_lines = self._calculate_visible_lines()
        page_size = max(MIN_PAGE_SCROLL, int(visible_lines * PAGE_SCROLL_FACTOR))
        self.scroll_up(page_size)

    def page_down(self) -> None:
        visible_lines = self._calculate_visible_lines()
        page_size = max(MIN_PAGE_SCROLL, int(visible_lines * PAGE_SCROLL_FACTOR))
        self.scroll_down(page_size)

    def scroll_to_top(self) -> None:
        self._view_top_index = 0
        self._auto_scroll = False
        if self._live:
            self._live.update(self._render())

    def scroll_to_bottom(self) -> None:
        self._auto_scroll = True
        self._paused = False
        if self._live:
            self._live.update(self._render())

    def toggle_pause(self) -> None:
        self._paused = not self._paused

        if self._paused and self._auto_scroll:
            visible_lines = self._calculate_visible_lines()
            total = len(self.log_messages)

            if total > visible_lines:
                self._view_top_index = total - visible_lines
            else:
                self._view_top_index = 0

            self._auto_scroll = False

        if self._live:
            self._live.update(self._render())
