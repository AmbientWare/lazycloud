"""CLI configuration for shared SDK progress and streamed logs."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lazycloud.cli.components.errors import debug_errors_enabled
from lazycloud.cli.components.output import write_stream
from lazycloud.terminal import Terminal


@runtime_checkable
class TerminalAware(Protocol):
    terminal: Terminal | None


@runtime_checkable
class TerminalResourceCollection(Protocol):
    @property
    def resources(self) -> tuple[object, ...]: ...


def attach_terminal(target: object, terminal: Terminal | None = None) -> Terminal:
    selected = terminal or Terminal(verbose=debug_errors_enabled())
    if isinstance(target, TerminalAware):
        target.terminal = selected
    if isinstance(target, TerminalResourceCollection):
        for resource in target.resources:
            attach_terminal(resource, selected)
    return selected


def print_stream_message(stream: str, message: str) -> None:
    write_stream(
        message if message.endswith("\n") else f"{message}\n",
        error=stream == "stderr",
    )


__all__ = ["attach_terminal", "print_stream_message"]
