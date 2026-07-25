from __future__ import annotations

import os
import pty
import termios
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.clients.shell.control import ShellControlClient
from lazycloud.terminal_shell import InteractiveShell, LocalShellTerminal, ShellConnectionError
from shared.http.errors import ErrorResponse, HttpApiError
from shared.http.shells import (
    CreateShellInExistingContainerResponse,
    CreateStandaloneShellResponse,
    ShellConnectPlanResponse,
)
from shared.shell_protocol import (
    ShellAuthRequest,
    ShellFrameDecoder,
    ShellFrameType,
    ShellResizeRequest,
    encode_shell_frame,
)


@dataclass
class FakeShellClient:
    fail: bool = False

    def create_standalone(self, stub_id: str) -> CreateStandaloneShellResponse:
        if self.fail:
            raise _http_error("standalone failed", status_code=404)
        return CreateStandaloneShellResponse(
            container_id=f"container-{stub_id}",
            username="user",
            password="pass",
            websocket_ticket="wst_standalone",
        )

    def create_existing(self, container_id: str) -> CreateShellInExistingContainerResponse:
        if self.fail:
            raise _http_error("existing failed", status_code=503)
        return CreateShellInExistingContainerResponse(
            username="user",
            password="pass",
            stub_id="stub-existing",
            websocket_ticket="wst_existing",
        )

    def connect_plan(self, stub_id: str, container_id: str) -> ShellConnectPlanResponse:
        if self.fail:
            raise _http_error("connect failed", status_code=404)
        return ShellConnectPlanResponse(
            route_path="/api/v1/shells/id",
            container_id=container_id,
            stub_id=stub_id,
            worker_port=2222,
            buffer_size_bytes=4096,
            keepalive_interval_seconds=30,
            dial_timeout_seconds=10,
        )


@dataclass
class RecordingShellChannel:
    get_paths: list[str]

    def get(self, path: str):
        self.get_paths.append(path)
        return ShellConnectPlanResponse(
            route_path="/api/v1/shells/id/stub-1/container-1",
            container_id="container-1",
            stub_id="stub-1",
            worker_port=2222,
            buffer_size_bytes=4096,
            keepalive_interval_seconds=30,
            dial_timeout_seconds=10,
        ).model_dump(mode="json")

    def post(self, path: str, payload: dict[str, Any] | None = None):
        raise AssertionError("connect_plan should use GET")


@dataclass
class FakeWebSocket:
    received: list[str | bytes]
    sent: list[bytes]

    def recv(self, timeout: float | None = None) -> str | bytes:
        _ = timeout
        if not self.received:
            raise TimeoutError
        return self.received.pop(0)

    def send(self, message: str | bytes) -> None:
        assert isinstance(message, bytes)
        self.sent.append(message)


@dataclass
class RecordingConnector:
    websocket: FakeWebSocket
    url: str = ""
    token: str = ""
    open_timeout_seconds: float = 0
    max_message_bytes: int = 0

    @contextmanager
    def __call__(
        self,
        url: str,
        *,
        token: str,
        open_timeout_seconds: float,
        max_message_bytes: int,
    ) -> Iterator[FakeWebSocket]:
        self.url = url
        self.token = token
        self.open_timeout_seconds = open_timeout_seconds
        self.max_message_bytes = max_message_bytes
        yield self.websocket


@dataclass
class FakeTerminal:
    reads: list[bytes | None]
    output: bytearray
    active: bool = False
    resize: bool = True

    @property
    def term(self) -> str:
        return "xterm-test"

    @property
    def signal_exit_code(self) -> int | None:
        return None

    @contextmanager
    def activate(self) -> Iterator[None]:
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def size(self) -> tuple[int, int]:
        return (120, 44)

    def read(self, timeout_seconds: float) -> bytes | None:
        _ = timeout_seconds
        return self.reads.pop(0) if self.reads else None

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    def take_resize(self) -> bool:
        resize = self.resize
        self.resize = False
        return resize


def test_shell_creates_sessions_and_connect_plan() -> None:
    shell = Shell(client=FakeShellClient())

    standalone = shell.create_standalone("stub-1")
    existing = shell.create_existing("container-1")
    plan = shell.connect_plan("stub-1", "container-1")

    assert standalone.container_id == "container-stub-1"
    assert standalone.stub_id == "stub-1"
    assert existing.container_id == "container-1"
    assert existing.stub_id == "stub-existing"
    assert plan.worker_port == 2222


def test_shell_propagates_http_api_errors() -> None:
    shell = Shell(client=FakeShellClient(fail=True))

    with pytest.raises(HttpApiError, match="standalone failed") as standalone_error:
        shell.create_standalone("stub-1")
    with pytest.raises(HttpApiError, match="existing failed") as existing_error:
        shell.create_existing("container-1")
    with pytest.raises(HttpApiError, match="connect failed"):
        shell.connect_plan("stub-1", "container-1")
    assert standalone_error.value.status_code == 404
    assert existing_error.value.status_code == 503


def test_shell_control_client_uses_explicit_connect_plan_route() -> None:
    channel = RecordingShellChannel(get_paths=[])
    client = ShellControlClient(channel=channel)

    plan = client.connect_plan("stub/1", "container 1")

    assert plan.route_path == "/api/v1/shells/id/stub-1/container-1"
    assert channel.get_paths == ["/api/v1/shells/connect-plan/stub%2F1/container%201"]


def test_interactive_shell_authenticates_resizes_streams_and_returns_exit_code() -> None:
    websocket = FakeWebSocket(
        received=[
            "OK",
            encode_shell_frame(ShellFrameType.Ready.value),
            encode_shell_frame(ShellFrameType.Data.value, b"remote output\r\n")
            + encode_shell_frame(ShellFrameType.Exit.value, b"7"),
        ],
        sent=[],
    )
    connector = RecordingConnector(websocket)
    terminal = FakeTerminal(reads=[b"echo test\n"], output=bytearray())
    client = InteractiveShell(connector=connector, terminal=terminal)

    exit_code = client.run(
        endpoint="https://control.example/base",
        token="test-token",
        credentials=ShellSession(
            container_id="container-1",
            stub_id="stub-1",
            username="root",
            password="secret",
        ),
        plan=FakeShellClient().connect_plan("stub-1", "container-1"),
        open_timeout_seconds=12,
    )

    assert exit_code == 7
    assert connector.url == "wss://control.example/base/api/v1/shells/id/ws"
    assert connector.token == "test-token"
    assert "test-token" not in connector.url
    assert connector.open_timeout_seconds == 12
    assert bytes(terminal.output) == b"remote output\r\n"
    assert terminal.active is False
    frames = [ShellFrameDecoder().feed(item)[0] for item in websocket.sent]
    assert [item.frame_type for item in frames] == [
        ShellFrameType.Auth.value,
        ShellFrameType.Resize.value,
        ShellFrameType.Data.value,
    ]
    auth = ShellAuthRequest.model_validate_json(frames[0].payload)
    resize = ShellResizeRequest.model_validate_json(frames[1].payload)
    assert (auth.username, auth.password, auth.term, auth.cols, auth.rows) == (
        "root",
        "secret",
        "xterm-test",
        120,
        44,
    )
    assert (resize.cols, resize.rows) == (120, 44)
    assert frames[2].payload == b"echo test\n"


def test_interactive_shell_fails_closed_on_backend_error() -> None:
    websocket = FakeWebSocket(
        received=[
            "OK",
            encode_shell_frame(ShellFrameType.Error.value, b"credentials rejected"),
        ],
        sent=[],
    )
    terminal = FakeTerminal(reads=[], output=bytearray())

    with pytest.raises(ShellConnectionError, match="credentials rejected"):
        InteractiveShell(
            connector=RecordingConnector(websocket),
            terminal=terminal,
        ).run(
            endpoint="http://control.example",
            token="test-token",
            credentials=ShellSession(
                container_id="container-1",
                stub_id="stub-1",
                username="root",
                password="wrong",
            ),
            plan=FakeShellClient().connect_plan("stub-1", "container-1"),
            open_timeout_seconds=10,
        )

    assert terminal.active is False


def test_local_shell_terminal_restores_tty_state() -> None:
    master_fd, terminal_fd = pty.openpty()
    initial = termios.tcgetattr(terminal_fd)
    termios.tcsetattr(terminal_fd, termios.TCSADRAIN, initial)
    before = termios.tcgetattr(terminal_fd)
    terminal = LocalShellTerminal(input_fd=terminal_fd, output_fd=terminal_fd)
    try:
        with terminal.activate():
            assert termios.tcgetattr(terminal_fd) != before
        after = termios.tcgetattr(terminal_fd)
        assert after[:3] == before[:3]
        assert after[4:] == before[4:]
        for flag in (termios.ECHO, termios.ICANON, termios.ISIG, termios.IEXTEN):
            assert after[3] & flag == before[3] & flag
    finally:
        os.close(terminal_fd)
        os.close(master_fd)


def test_shell_session_repr_does_not_expose_password() -> None:
    session = ShellSession(
        container_id="container-1",
        stub_id="stub-1",
        username="root",
        password="do-not-print",
    )

    assert "do-not-print" not in repr(session)


def _http_error(detail: str, *, status_code: int) -> HttpApiError:
    return HttpApiError(
        detail,
        status_code=status_code,
        error=ErrorResponse(detail=detail),
    )
