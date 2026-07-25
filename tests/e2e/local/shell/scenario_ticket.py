"""Consume one browser shell ticket and prove that it cannot be replayed.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates one unique app, cancels its hold-open task, and
publicly deletes the app.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from lazycloud.cli.control import resource_client
from lazycloud.session.task import FunctionCall
from lazycloud.terminal_shell import shell_websocket_url
from shared.http.shells import (
    CreateShellInExistingContainerRequest,
    CreateShellInExistingContainerResponse,
    ShellConnectPlanResponse,
)
from shared.http_transport import HttpChannel
from shared.shell_protocol import (
    SHELL_DEFAULT_TERM,
    ShellAuthRequest,
    ShellFrameDecoder,
    ShellFrameType,
    encode_shell_frame,
)
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

SOURCE_ROOT = Path(__file__).resolve().parent
TERMINAL = {"complete", "failed", "cancelled", "timeout", "expired"}


def _await_container(call: FunctionCall[str], *, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        task = call.task.view()
        if task.status.value == "running" and task.container_id:
            return task.container_id
        if task.status.value in TERMINAL:
            raise RuntimeError(
                f"ticket target reached {task.status.value} before it was attachable"
            )
        time.sleep(0.25)
    raise RuntimeError("ticket target did not become attachable before timeout")


def _ticket_url(endpoint: str, route_path: str, ticket: str) -> str:
    parsed = urlsplit(shell_websocket_url(endpoint, route_path))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode({"ticket": ticket}), "")
    )


def _consume_ticket(
    endpoint: str,
    session: CreateShellInExistingContainerResponse,
    plan: ShellConnectPlanResponse,
    marker: str,
) -> None:
    decoder = ShellFrameDecoder()
    output = bytearray()
    command_sent = False
    deadline = time.monotonic() + 30
    with connect(
        _ticket_url(endpoint, plan.route_path, session.websocket_ticket),
        open_timeout=30,
        max_size=2 * 1024 * 1024,
    ) as websocket:
        if websocket.recv(timeout=30) != "OK":
            raise RuntimeError("browser shell did not receive the proxy preface")
        auth = ShellAuthRequest(
            username=session.username,
            password=session.password,
            term=SHELL_DEFAULT_TERM,
            cols=80,
            rows=24,
        )
        websocket.send(
            encode_shell_frame(
                ShellFrameType.Auth.value,
                auth.model_dump_json().encode("utf-8"),
            )
        )
        while time.monotonic() < deadline:
            message = websocket.recv(timeout=max(deadline - time.monotonic(), 0.1))
            if not isinstance(message, bytes):
                raise RuntimeError("browser shell returned an unexpected text frame")
            for frame in decoder.feed(message):
                if frame.frame_type == ShellFrameType.Ready.value:
                    if command_sent:
                        raise RuntimeError("browser shell emitted duplicate readiness")
                    websocket.send(
                        encode_shell_frame(
                            ShellFrameType.Data.value,
                            f"printf '{marker}\\n'\nexit\n".encode(),
                        )
                    )
                    command_sent = True
                elif frame.frame_type == ShellFrameType.Data.value:
                    output.extend(frame.payload)
                elif frame.frame_type == ShellFrameType.Error.value:
                    raise RuntimeError("browser shell backend rejected the session")
                elif frame.frame_type == ShellFrameType.Exit.value:
                    if frame.payload != b"0" or marker.encode() not in output:
                        raise RuntimeError("browser shell returned an invalid terminal outcome")
                    return
        raise RuntimeError("browser shell did not exit before timeout")


def _assert_replay_rejected(endpoint: str, plan: ShellConnectPlanResponse, ticket: str) -> None:
    try:
        with connect(_ticket_url(endpoint, plan.route_path, ticket), open_timeout=10) as websocket:
            websocket.recv(timeout=2)
    except (ConnectionClosed, InvalidHandshake):
        return
    raise RuntimeError("consumed browser shell ticket was accepted twice")


def _cleanup(call: FunctionCall[str] | None, workspace: str, app_name: str) -> None:
    if call is not None and call.task.view().status.value not in TERMINAL:
        call.cancel()
        call.task.wait(timeout_seconds=60, poll_interval_seconds=0.25)
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == app_name]
    if len(matches) > 1:
        raise RuntimeError("unique ticket app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == app_name for item in client.list_apps(active=True).data):
        raise RuntimeError("ticket app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "browser shell ticket")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_ticket import APP_NAME, app, hold_ticket_target

    endpoint = profile.resolved_endpoint().rstrip("/")
    workspace = profile.workspace
    call: FunctionCall[str] | None = None
    try:
        app.deploy(
            workspace=workspace,
            source_root=SOURCE_ROOT,
            env={"LAZYCLOUD_E2E_APP": APP_NAME},
        )
        call = hold_ticket_target.spawn(120.0)
        container_id = _await_container(call, timeout_seconds=60)
        channel = HttpChannel(endpoint=endpoint, token=profile.token)
        session = CreateShellInExistingContainerResponse.model_validate(
            channel.post(
                "/api/v1/shells/existing-container",
                CreateShellInExistingContainerRequest(container_id=container_id).model_dump(
                    mode="json"
                ),
            )
        )
        plan = ShellConnectPlanResponse.model_validate(
            channel.get(
                f"/api/v1/shells/connect-plan/{quote(hold_ticket_target.stub_id or '', safe='')}/"
                f"{quote(container_id, safe='')}"
            )
        )
        marker = f"shell-ticket-{time.time_ns()}"
        _consume_ticket(endpoint, session, plan, marker)
        _assert_replay_rejected(endpoint, plan, session.websocket_ticket)
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "shell.ticket-single-use",
                    "container_id": container_id,
                    "task_id": call.task_id,
                }
            )
        )
    finally:
        _cleanup(call, workspace, APP_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
