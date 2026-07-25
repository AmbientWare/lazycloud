from __future__ import annotations

from dataclasses import dataclass, field

from lazycloud.cli.components import output, theme
from lazycloud.terminal import Terminal
from rich.console import Console
from rich.style import Style

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
    console: Console = field(default_factory=lambda: output.console)
    error_console: Console = field(default_factory=lambda: output.error_console)
    _pending: str = ""

    def write(self, message: str) -> None:
        if self.quiet or not message:
            return
        self._pending += message.replace("\r", "\n")
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", maxsplit=1)
            self._print_cli_line(line)

    def line(self, message: str = "") -> None:
        if self.quiet:
            return
        self._flush_pending()
        self._print_cli_line(message)

    def error(self, message: str) -> None:
        if self.quiet:
            return
        self._flush_pending()
        self.error_console.print(theme.styled(message, theme.ERROR + theme.EMPHASIS))

    def header(self, message: str) -> None:
        self.line(message)

    def detail(self, message: str) -> None:
        self.line(message)

    def warn(self, message: str) -> None:
        if self.quiet:
            return
        self._flush_pending()
        self.console.print(theme.styled(message, theme.WARNING))

    def success(self, message: str) -> None:
        if self.quiet:
            return
        self._flush_pending()
        self.console.print(theme.styled(message, theme.SUCCESS + theme.EMPHASIS))

    def _flush_pending(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self._print_cli_line(pending)

    def _print_cli_line(self, message: str) -> None:
        if not message:
            self.console.print()
            return
        self.console.print(theme.styled(message, cli_output_style(message)))


def cli_output_style(message: str) -> Style:
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


def print_stream_message(
    stream: str,
    message: str,
    *,
    console: Console,
) -> None:
    style = theme.ERROR if stream == "stderr" else theme.PLAIN
    lines = message.splitlines() or [""]
    for line in lines:
        console.print(theme.styled(line, style))


__all__ = [
    "CliTerminal",
    "cli_output_style",
    "print_stream_message",
]
