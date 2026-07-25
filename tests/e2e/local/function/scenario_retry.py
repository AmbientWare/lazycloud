"""Observe a retried Function reach its public failed task state.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from lazycloud.session.task import TaskOperationError
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique retry app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("retry app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function retry")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_retry import APP_NAME, app, intentional_failure

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        call = intentional_failure.spawn("accepted")
        try:
            call.get(timeout_seconds=120, poll_interval_seconds=0.5)
        except TaskOperationError:
            pass
        else:
            raise RuntimeError("intentional Function failure unexpectedly succeeded")
        task = call.task.wait(timeout_seconds=30, poll_interval_seconds=0.25)
        if task.status.value != "failed":
            raise RuntimeError("retried Function did not reach failed")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.retry",
                    "status": task.status.value,
                    "task_id": call.task_id,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
