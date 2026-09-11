from __future__ import annotations

import os
import select
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from shared.http.shells import ShellConnectPlanResponse
from shared.shell_protocol import (
    SHELL_DEFAULT_TERM,
    SHELL_FRAME_HEADER_SIZE,
    SHELL_FRAME_MAX_PAYLOAD_BYTES,
    ShellAuthRequest,
    ShellFrameDecoder,
    ShellFrameType,
    ShellResizeRequest,
    encode_shell_frame,
)


class ShellConnectionError(RuntimeError):
    pass


class ShellCredentials(Protocol):
    @property
    def username(self) -> str: ...

    @property
    def password(self) -> str: ...


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
class _ServerState:
    ready: bool = False
    exit_code: int | None = None


@dataclass(slots=True)
class InteractiveShell:
    connector: ShellWebSocketConnector = field(default_factory=lambda: _connect_websocket)
    terminal: ShellTerminal = field(default_factory=LocalShellTerminal)
    poll_interval_seconds: float = 0.025

    def run(
        self,
        *,
        endpoint: str,
        token: str | None,
        credentials: ShellCredentials,
        plan: ShellConnectPlanResponse,
        open_timeout_seconds: float,
    ) -> int:
        from websockets.exceptions import ConnectionClosed

        if not token:
            msg = "an authenticated profile token is required to open a shell"
            raise ShellConnectionError(msg)
        url = shell_websocket_url(endpoint, plan.route_path)
        max_message_bytes = max(
            plan.buffer_size_bytes,
            SHELL_FRAME_HEADER_SIZE + SHELL_FRAME_MAX_PAYLOAD_BYTES,
        )
        try:
            with self.connector(
                url,
                token=token,
                open_timeout_seconds=open_timeout_seconds,
                max_message_bytes=max_message_bytes,
            ) as websocket:
                self._wait_for_proxy(websocket, open_timeout_seconds)
                columns, rows = self.terminal.size()
                auth = ShellAuthRequest(
                    username=credentials.username,
                    password=credentials.password,
                    term=self.terminal.term,
                    cols=columns,
                    rows=rows,
                )
                websocket.send(
                    encode_shell_frame(
                        ShellFrameType.Auth.value,
                        auth.model_dump_json().encode("utf-8"),
                    )
                )
                return self._bridge(websocket, open_timeout_seconds)
        except ShellConnectionError:
            raise
        except ConnectionClosed as exc:
            reason = exc.rcvd.reason if exc.rcvd is not None else "connection lost"
            msg = f"shell connection closed before the process exited: {reason}"
            raise ShellConnectionError(msg) from exc
        except OSError as exc:
            raise ShellConnectionError(f"shell connection failed: {exc}") from exc

    def _wait_for_proxy(self, websocket: ShellWebSocket, timeout_seconds: float) -> None:
        preface = websocket.recv(timeout=timeout_seconds)
        if not isinstance(preface, str) or preface != "OK":
            msg = "shell proxy did not confirm the backend connection"
            raise ShellConnectionError(msg)

    def _bridge(self, websocket: ShellWebSocket, ready_timeout_seconds: float) -> int:
        decoder = ShellFrameDecoder()
        state = _ServerState()
        self._receive_until_ready(websocket, decoder, state, ready_timeout_seconds)
        if state.exit_code is not None:
            return state.exit_code

        input_open = True
        with self.terminal.activate():
            while state.exit_code is None:
                signal_exit_code = self.terminal.signal_exit_code
                if signal_exit_code is not None:
                    return signal_exit_code
                if self.terminal.take_resize():
                    self._send_resize(websocket)
                if input_open:
                    data = self.terminal.read(self.poll_interval_seconds)
                    if data == b"":
                        input_open = False
                    elif data:
                        websocket.send(encode_shell_frame(ShellFrameType.Data.value, data))
                self._receive_available(websocket, decoder, state)
        return state.exit_code

    def _receive_until_ready(
        self,
        websocket: ShellWebSocket,
        decoder: ShellFrameDecoder,
        state: _ServerState,
        timeout_seconds: float,
    ) -> None:
        while not state.ready and state.exit_code is None:
            try:
                message = websocket.recv(timeout=timeout_seconds)
            except TimeoutError as exc:
                raise ShellConnectionError("shell authentication timed out") from exc
            self._consume_message(message, decoder, state)

    def _receive_available(
        self,
        websocket: ShellWebSocket,
        decoder: ShellFrameDecoder,
        state: _ServerState,
    ) -> None:
        while state.exit_code is None:
            try:
                message = websocket.recv(timeout=0)
            except TimeoutError:
                return
            self._consume_message(message, decoder, state)

    def _consume_message(
        self,
        message: str | bytes,
        decoder: ShellFrameDecoder,
        state: _ServerState,
    ) -> None:
        if isinstance(message, str):
            msg = "shell backend sent an unexpected text message"
            raise ShellConnectionError(msg)
        for frame in decoder.feed(message):
            if frame.frame_type == ShellFrameType.Ready.value:
                state.ready = True
            elif frame.frame_type == ShellFrameType.Data.value:
                self.terminal.write(frame.payload)
            elif frame.frame_type == ShellFrameType.Error.value:
                detail = frame.payload.decode("utf-8", errors="replace") or "shell error"
                raise ShellConnectionError(detail)
            elif frame.frame_type == ShellFrameType.Exit.value:
                state.exit_code = _exit_code(frame.payload)
            else:
                msg = f"shell backend sent unknown frame type {frame.frame_type!r}"
                raise ShellConnectionError(msg)

    def _send_resize(self, websocket: ShellWebSocket) -> None:
        columns, rows = self.terminal.size()
        resize = ShellResizeRequest(cols=columns, rows=rows)
        websocket.send(
            encode_shell_frame(
                ShellFrameType.Resize.value,
                resize.model_dump_json().encode("utf-8"),
            )
        )


def shell_websocket_url(endpoint: str, route_path: str) -> str:
    raw = f"{endpoint.rstrip('/')}/{route_path.lstrip('/')}/ws"
    parsed = urlsplit(raw)
    if parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme == "https":
        scheme = "wss"
    else:
        msg = f"unsupported control endpoint scheme: {parsed.scheme or '<missing>'}"
        raise ShellConnectionError(msg)
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _exit_code(payload: bytes) -> int:
    try:
        value = int(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ShellConnectionError("shell backend sent an invalid exit code") from exc
    if not 0 <= value <= 255:
        raise ShellConnectionError("shell backend exit code is outside the valid range")
    return value


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
    "InteractiveShell",
    "LocalShellTerminal",
    "ShellConnectionError",
    "ShellTerminal",
    "ShellWebSocket",
    "ShellWebSocketConnector",
    "shell_websocket_url",
]
