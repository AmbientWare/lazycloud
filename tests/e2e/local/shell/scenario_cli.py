"""Run one command in a live Function container through `lazycloud shell`.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates one unique app, cancels its hold-open task, and
publicly deletes the app.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from lazycloud.session.task import FunctionCall
from tests.e2e._support.process import (
    LivePrerequisiteError,
    blocked,
    require_live,
    run_text_process,
)

ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = Path(__file__).resolve().parent
TERMINAL = {"complete", "failed", "cancelled", "timeout", "expired"}


def _await_container(call: FunctionCall[str], *, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        task = call.task.view()
        if task.status.value == "running" and task.container_id:
            return task.container_id
        if task.status.value in TERMINAL:
            raise RuntimeError(f"shell target reached {task.status.value} before it was attachable")
        time.sleep(0.25)
    raise RuntimeError("shell target did not become attachable before timeout")


def _cleanup(call: FunctionCall[str] | None, workspace: str, app_name: str) -> None:
    if call is not None and call.task.view().status.value not in TERMINAL:
        call.cancel()
        call.task.wait(timeout_seconds=60, poll_interval_seconds=0.25)
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == app_name]
    if len(matches) > 1:
        raise RuntimeError("unique CLI shell app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == app_name for item in client.list_apps(active=True).data):
        raise RuntimeError("CLI shell app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "CLI shell")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_cli import APP_NAME, app, hold_shell_target

    workspace = profile.workspace
    call: FunctionCall[str] | None = None
    try:
        app.deploy(
            workspace=workspace,
            source_root=SOURCE_ROOT,
            env={"LAZYCLOUD_E2E_APP": APP_NAME},
        )
        call = hold_shell_target.spawn(120.0)
        container_id = _await_container(call, timeout_seconds=60)
        marker = f"shell-cli-{time.time_ns()}"
        result = run_text_process(
            (
                "uv",
                "run",
                "lazycloud",
                "shell",
                "--container-id",
                container_id,
                "--workspace",
                workspace,
            ),
            cwd=ROOT,
            environment=os.environ,
            stdin=f"printf '{marker}\\n'\nexit\n",
            timeout=120,
            secrets=(profile.token,),
        )
        if marker not in result.stdout:
            raise RuntimeError("CLI shell output omitted its terminal marker")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "shell.cli",
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
