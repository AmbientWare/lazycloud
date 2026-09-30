"""CLI configuration for shared SDK progress and streamed logs."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError

from lazycloud.cli.components.errors import debug_errors_enabled
from lazycloud.cli.components.output import write_stream
from lazycloud.terminal import Terminal, TerminalStep

CONNECTING_POLL_SECONDS = 1.0


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


@dataclass
class ConnectingIndicator:
    """A spinner on stderr while a connection waits for the machine behind it.

    Shown only when stderr is a terminal: an editor running the SSH proxy gives
    it none, and a line written there would reach nobody. `describe` is asked
    about once a second for what the machine is doing, and returns None when it
    has nothing to say.
    """

    target: str
    describe: Callable[[], str | None] | None = None
    _step: TerminalStep | None = field(default=None, init=False)
    _stopped: threading.Event = field(default_factory=threading.Event, init=False)

    def start(self) -> ConnectingIndicator:
        if not sys.stderr.isatty():
            return self
        self._step = Terminal().step("Connecting", self.target)
        if self.describe is not None:
            threading.Thread(target=self._follow, daemon=True).start()
        return self

    def connected(self) -> None:
        """Erase the spinner, before the connection's first output reaches the terminal."""
        self._stopped.set()
        if self._step is not None:
            self._step.dismiss()

    def failed(self, reason: str) -> bool:
        """Replace the spinner with the reason; False when there was no spinner to replace."""
        self._stopped.set()
        if self._step is None or self._step.finished:
            return False
        self._step.fail(f"{self.target} · {reason}")
        return True

    def _follow(self) -> None:
        describe = self.describe
        while describe is not None and not self._stopped.wait(CONNECTING_POLL_SECONDS):
            try:
                detail = describe()
            except (HttpApiError, HttpTransportError, HttpResponseDecodeError):
                # A status read that fails leaves the label as it was; it must
                # never end the connection it describes.
                continue
            step = self._step
            if detail and step is not None and not self._stopped.is_set():
                step.update(f"{self.target} · {detail}")


__all__ = ["ConnectingIndicator", "attach_terminal", "print_stream_message"]
