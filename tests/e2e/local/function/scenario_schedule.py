"""Observe one scheduled Function execution through the public task API.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import Client

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique schedule app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("schedule app remained active after deletion")


def _await_scheduled_task(stub_id: str, *, timeout_seconds: float) -> str:
    client = Client()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for task in client.tasks(limit=100):
            if task.stub_id == stub_id and task.status.value == "complete":
                return task.id
        time.sleep(0.5)
    raise RuntimeError("scheduled Function did not complete before timeout")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function schedule")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_schedule import APP_NAME, app, scheduled_marker

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        if not scheduled_marker.stub_id:
            raise RuntimeError("scheduled Function deployment omitted its stub ID")
        task_id = _await_scheduled_task(scheduled_marker.stub_id, timeout_seconds=180)
        print(json.dumps({"app": APP_NAME, "capability": "function.schedule", "task_id": task_id}))
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
