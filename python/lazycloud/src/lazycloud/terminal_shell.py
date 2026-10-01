from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Protocol

from pydantic import BaseModel, StrictInt, ValidationError

# The server's default when a client names no terminal type.
SHELL_DEFAULT_TERM = "xterm-256color"
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class ShellConnectionError(RuntimeError):
    pass


class ShellWebSocket(Protocol):
    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def send(self, message: str | bytes) -> None: ...


class ShellWebSocketConnector(Protocol):
    def __call__(
        self,
        url: str,
        *,
        token: str,
        open_timeout_seconds: float,
        max_message_bytes: int,
    ) -> AbstractContextManager[ShellWebSocket]: ...


class ShellTerminal(Protocol):
    @property
    def term(self) -> str: ...

    @property
    def signal_exit_code(self) -> int | None: ...

    def activate(self) -> AbstractContextManager[None]: ...

    def size(self) -> tuple[int, int]: ...

    def read(self, timeout_seconds: float) -> bytes | None: ...

    def write(self, data: bytes) -> None: ...

    def take_resize(self) -> bool: ...


@dataclass(slots=True)
class LocalShellTerminal:
    input_fd: int = field(default_factory=lambda: sys.stdin.fileno())
    output_fd: int = field(default_factory=lambda: sys.stdout.fileno())
    _resize_requested: bool = field(default=False, init=False)
    _signal_exit_code: int | None = field(default=None, init=False)

    @property
    def term(self) -> str:
        return os.environ.get("TERM", SHELL_DEFAULT_TERM)

    @property
    def signal_exit_code(self) -> int | None:
        return self._signal_exit_code

    @contextmanager
    def activate(self) -> Iterator[None]:
        is_tty = os.isatty(self.input_fd)
        previous_attributes = self._enter_raw_mode() if is_tty and os.name == "posix" else None
        watched_signals = [signal.SIGINT, signal.SIGTERM]
        resize_signal = None
        if os.name == "posix":
            watched_signals.extend([signal.SIGHUP, signal.SIGWINCH])
            resize_signal = signal.SIGWINCH
        previous_handlers = [(item, signal.getsignal(item)) for item in watched_signals]
        self._resize_requested = True
        self._signal_exit_code = None
        try:
            for item in watched_signals:
                handler = self._handle_resize if item == resize_signal else self._handle_exit
                signal.signal(item, handler)
            yield
        finally:
            if previous_attributes is not None:
                import termios

                termios.tcsetattr(self.input_fd, termios.TCSADRAIN, previous_attributes)
            for item, handler in previous_handlers:
                signal.signal(item, handler)

    def size(self) -> tuple[int, int]:
        try:
            dimensions = os.get_terminal_size(self.input_fd)
        except OSError:
            return (80, 24)
        return (max(dimensions.columns, 1), max(dimensions.lines, 1))

    def read(self, timeout_seconds: float) -> bytes | None:
        if os.name == "nt":
            return self._read_windows(timeout_seconds)
        readable, _, _ = select.select([self.input_fd], [], [], timeout_seconds)
        if not readable:
            return None
        return os.read(self.input_fd, 64 * 1024)

    def write(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(self.output_fd, remaining)
            remaining = remaining[written:]

    def take_resize(self) -> bool:
        requested = self._resize_requested
        self._resize_requested = False
        return requested

    def _handle_resize(self, _signum: int, _frame: FrameType | None) -> None:
        self._resize_requested = True

    def _handle_exit(self, signum: int, _frame: FrameType | None) -> None:
        self._signal_exit_code = 128 + signum

    def _enter_raw_mode(self) -> list[int | list[bytes]]:
        import termios
        import tty

        previous_attributes = termios.tcgetattr(self.input_fd)
        tty.setraw(self.input_fd)
        return previous_attributes

    def _read_windows(self, timeout_seconds: float) -> bytes | None:
        import msvcrt

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                character = msvcrt.getwch()
                if character in {"\x00", "\xe0"}:
                    character += msvcrt.getwch()
                return character.encode("utf-8")
            time.sleep(min(0.01, timeout_seconds))
        return None


@dataclass(slots=True)
class InteractiveShell:
    """Bridges the local terminal to a container shell over the API's WebSocket.

    Binary messages carry terminal bytes both ways. The client sends a text
    `{"type": "resize"}` when the window changes; the server sends a text
    `{"type": "exit", "code": N}` when the shell ends, or `{"type": "error"}`.
    """

    connector: ShellWebSocketConnector = field(default_factory=lambda: _connect_websocket)
    terminal: ShellTerminal = field(default_factory=LocalShellTerminal)
    poll_interval_seconds: float = 0.025
    on_attached: Callable[[], None] | None = None
    """Runs once, when the shell's socket opens and before any output."""

    def run(
        self,
        *,
        url: Callable[[int, int, str], str],
        token: str | None,
        open_timeout_seconds: float,
        check_health: Callable[[], None] | None = None,
    ) -> int:
        """Run the shell at `url(cols, rows, term)` until it exits; returns its exit code."""
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        if not token:
            msg = "an authenticated profile token is required to open a shell"
            raise ShellConnectionError(msg)
        columns, rows = self.terminal.size()
        try:
            with self.connector(
                url(columns, rows, self.terminal.term),
                token=token,
                open_timeout_seconds=open_timeout_seconds,
                max_message_bytes=_MAX_MESSAGE_BYTES,
            ) as websocket:
                self._attached()
                return self._bridge(websocket, check_health)
        except ShellConnectionError:
            raise
        except InvalidStatus as exc:
            raise ShellConnectionError(f"shell connection failed: {_refusal(exc)}") from exc
        except ConnectionClosed as exc:
            reason = exc.rcvd.reason if exc.rcvd is not None and exc.rcvd.reason else None
            msg = (
                f"shell connection closed before the process exited: {reason or 'connection lost'}"
            )
            raise ShellConnectionError(msg) from exc
        except OSError as exc:
            raise ShellConnectionError(f"shell connection failed: {exc}") from exc

    def _bridge(self, websocket: ShellWebSocket, check_health: Callable[[], None] | None) -> int:
        state = _ServerState()
        input_open = True
        with self.terminal.activate():
            while state.exit_code is None:
                if check_health is not None:
                    check_health()
                signal_exit_code = self.terminal.signal_exit_code
                if signal_exit_code is not None:
                    return signal_exit_code
                if self.terminal.take_resize():
                    columns, rows = self.terminal.size()
                    message = json.dumps({"type": "resize", "cols": columns, "rows": rows})
                    self._send(websocket, message, state)
                if input_open:
                    data = self.terminal.read(self.poll_interval_seconds)
                    if data == b"":
                        input_open = False
                    elif data:
                        self._send(websocket, data, state)
                else:
                    time.sleep(self.poll_interval_seconds)
                self._receive_available(websocket, state)
        return state.exit_code

    def _send(self, websocket: ShellWebSocket, message: str | bytes, state: _ServerState) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            websocket.send(message)
        except ConnectionClosed:
            # The server may close right after its exit or error message; read
            # what it sent before reporting the closed connection.
            self._receive_available(websocket, state)
            if state.exit_code is None:
                raise

    def _receive_available(self, websocket: ShellWebSocket, state: _ServerState) -> None:
        while state.exit_code is None:
            try:
                message = websocket.recv(timeout=0)
            except TimeoutError:
                return
            if isinstance(message, bytes):
                self.terminal.write(message)
                continue
            state.exit_code = _control_message(message)

    def _attached(self) -> None:
        attached, self.on_attached = self.on_attached, None
        if attached is not None:
            attached()


@dataclass(slots=True)
class _ServerState:
    exit_code: int | None = None


class _ShellControl(BaseModel):
    """A text message of the shell protocol: an exit or an error."""

    type: str
    message: str | None = None
    code: StrictInt | None = None


def _control_message(text: str) -> int:
    """The exit code a server text message carries; an error message raises."""
    try:
        message = _ShellControl.model_validate_json(text)
    except ValidationError as exc:
        raise ShellConnectionError("shell backend sent an invalid text message") from exc
    if message.type == "error":
        raise ShellConnectionError(message.message or "shell error")
    if message.type != "exit":
        raise ShellConnectionError(f"shell backend sent an unknown message type {message.type!r}")
    code = message.code
    if code is None:
        raise ShellConnectionError("shell backend sent an invalid exit code")
    if not 0 <= code <= 255:
        raise ShellConnectionError("shell backend exit code is outside the valid range")
    return code


def _refusal(exc: Exception) -> str:
    """The API's error message from a refused WebSocket handshake."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        message = json.loads(getattr(response, "body", b"") or b"").get("message")
    except (ValueError, AttributeError):
        message = None
    if message:
        return str(message)
    return f"HTTP {status}" if status else str(exc)


@contextmanager
def _connect_websocket(
    url: str,
    *,
    token: str,
    open_timeout_seconds: float,
    max_message_bytes: int,
) -> Iterator[ShellWebSocket]:
    from websockets.sync.client import connect

    with connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=open_timeout_seconds,
        close_timeout=5,
        max_size=max_message_bytes,
        compression=None,
    ) as websocket:
        yield websocket


__all__ = [
    "SHELL_DEFAULT_TERM",
    "InteractiveShell",
    "LocalShellTerminal",
    "ShellConnectionError",
    "ShellTerminal",
    "ShellWebSocket",
    "ShellWebSocketConnector",
]
