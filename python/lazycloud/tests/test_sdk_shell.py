from __future__ import annotations

import json
import os
import pty
import termios
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field

import pytest
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.terminal_shell import (
    InteractiveShell,
    LocalShellTerminal,
    ShellConnectionError,
    ShellWebSocket,
    _connect_websocket,
)
from websockets.sync.server import ServerConnection, serve

from tests.api_server import TOKEN, ApiRequest, FakeApi, Reply, error_reply, json_reply

CONTAINER = "0192f0a0-0000-7000-8000-0000000000c1"
RELEASE = "0192f0a0-0000-7000-8000-0000000000a1"
NOW = "2026-10-01T12:00:00Z"


def instance(state: str = "ready") -> dict[str, object]:
    return {
        "id": CONTAINER,
        "release_id": RELEASE,
        "app": "tools",
        "name": "web",
        "kind": "pod",
        "state": state,
        "created_at": NOW,
    }


@dataclass
class FakeTerminal:
    typed: list[bytes] = field(default_factory=lambda: [b"echo hi\r"])
    written: bytearray = field(default_factory=bytearray)
    resized: bool = True

    @property
    def term(self) -> str:
        return "xterm"

    @property
    def signal_exit_code(self) -> int | None:
        return None

    def activate(self) -> AbstractContextManager[None]:
        return nullcontext()

    def size(self) -> tuple[int, int]:
        return (100, 30)

    def read(self, timeout_seconds: float) -> bytes | None:
        return self.typed.pop(0) if self.typed else None

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def take_resize(self) -> bool:
        resized, self.resized = self.resized, False
        return resized


@contextmanager
def shell_server(handler: Callable[[ServerConnection], None]) -> Iterator[tuple[str, list[str]]]:
    """A WebSocket server standing in for the API's shell route; yields its URL and paths."""
    paths: list[str] = []

    def accept(connection: ServerConnection) -> None:
        assert connection.request is not None
        assert connection.request.headers["Authorization"] == f"Bearer {TOKEN}"
        paths.append(connection.request.path)
        handler(connection)

    with serve(accept, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"ws://127.0.0.1:{server.socket.getsockname()[1]}", paths
        finally:
            server.shutdown()
            thread.join()


def redirect_to(
    url: str, opened: list[str]
) -> Callable[..., AbstractContextManager[ShellWebSocket]]:
    def connector(target: str, **kwargs: object) -> AbstractContextManager[ShellWebSocket]:
        opened.append(target)
        path = target.split("/", 3)[3]
        return _connect_websocket(f"{url}/{path}", **kwargs)  # type: ignore[arg-type]

    return connector


def test_shell_waits_for_its_container_and_bridges_bytes_resizes_and_exit(
    fake_api: FakeApi,
) -> None:
    attempts: list[int] = []

    @fake_api.route("POST", f"/v1/workspaces/team/containers/{CONTAINER}/connect")
    def connect(request: ApiRequest) -> Reply:
        attempts.append(1)
        if len(attempts) == 1:
            return error_reply("unavailable", "container is starting", 503)
        return json_reply(instance())

    received: list[object] = []

    def handler(connection: ServerConnection) -> None:
        connection.send(b"$ ")
        received.append(json.loads(connection.recv()))
        received.append(connection.recv())
        connection.send(b"hi\r\n")
        connection.send(json.dumps({"type": "exit", "code": 3}))

    terminal = FakeTerminal()
    opened: list[str] = []
    attached: list[bool] = []
    with shell_server(handler) as (url, paths):
        shell = Shell(
            interactive_shell=InteractiveShell(
                connector=redirect_to(url, opened),
                terminal=terminal,
                on_attached=lambda: attached.append(True),
            )
        )
        status = shell.connect(ShellSession(container_id=CONTAINER, stub_id=RELEASE))

    assert status == 3
    assert len(attempts) == 2
    assert fake_api.calls("POST", f"/v1/workspaces/team/containers/{CONTAINER}/connect")[
        0
    ].query == {"wait_seconds": ["60"]}
    assert opened == [
        f"{fake_api.url.replace('http', 'ws')}/v1/workspaces/team/containers/{CONTAINER}/shell"
        "?cols=100&rows=30&term=xterm"
    ]
    assert paths == [
        f"/v1/workspaces/team/containers/{CONTAINER}/shell?cols=100&rows=30&term=xterm"
    ]
    assert received == [{"type": "resize", "cols": 100, "rows": 30}, b"echo hi\r"]
    assert bytes(terminal.written) == b"$ hi\r\n"
    assert attached == [True]


def test_shell_reports_the_servers_error_and_a_stopped_container(fake_api: FakeApi) -> None:
    fake_api.route("POST", f"/v1/workspaces/team/containers/{CONTAINER}/connect")(
        lambda request: json_reply(instance())
    )

    def handler(connection: ServerConnection) -> None:
        connection.send(json.dumps({"type": "error", "message": "no shell in this image"}))

    with shell_server(handler) as (url, _):
        shell = Shell(
            interactive_shell=InteractiveShell(
                connector=redirect_to(url, []), terminal=FakeTerminal()
            )
        )
        with pytest.raises(ShellConnectionError, match="no shell in this image"):
            shell.connect(ShellSession(container_id=CONTAINER, stub_id=RELEASE))

    fake_api.route("POST", f"/v1/workspaces/team/containers/{CONTAINER}/connect")(
        lambda request: error_reply("conflict", "the container stopped: crashed", 409)
    )
    with pytest.raises(ShellConnectionError, match="stopped: the container stopped: crashed"):
        Shell().connect(ShellSession(container_id=CONTAINER, stub_id=RELEASE))


def test_standalone_shells_start_a_shell_instance_of_the_release(fake_api: FakeApi) -> None:
    fake_api.route("POST", "/v1/workspaces/team/instances")(
        lambda request: json_reply(instance("pending"), 201)
    )
    fake_api.route("GET", f"/v1/workspaces/team/containers/{CONTAINER}")(
        lambda request: json_reply(
            {
                **instance(),
                "function": "web",
                "slots": 1,
                "running_tasks": 0,
                "cpu_millis": 1000,
                "memory_mib": 128,
            }
        )
    )

    standalone = Shell().create_standalone(RELEASE)
    existing = Shell().create_existing(CONTAINER)

    assert fake_api.calls("POST", "/v1/workspaces/team/instances")[0].json() == {
        "release_id": RELEASE,
        "shell": True,
    }
    assert (standalone.container_id, standalone.stub_id) == (CONTAINER, RELEASE)
    assert (existing.container_id, existing.stub_id) == (CONTAINER, RELEASE)


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
