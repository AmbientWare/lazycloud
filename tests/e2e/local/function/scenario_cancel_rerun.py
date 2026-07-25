"""Cancel a running Function task and rerun it through the public SDK.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from lazycloud.session.task import FunctionCall
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique cancel/rerun app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("cancel/rerun app remained active after deletion")


def _await_running(call: FunctionCall[int], *, timeout_seconds: float) -> None:
    task = call.task
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = task.view().status.value
        if status == "running":
            return
        if status in {"complete", "failed", "cancelled", "timeout", "expired"}:
            raise RuntimeError(f"Function reached {status} before cancellation")
        time.sleep(0.25)
    raise RuntimeError("Function did not reach running before cancellation")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function cancel and rerun")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_cancel_rerun import APP_NAME, app, delayed_square

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        cancelled = delayed_square.spawn(9, delay_seconds=8)
        _await_running(cancelled, timeout_seconds=60)
        cancelled.cancel()
        terminal = cancelled.task.wait(timeout_seconds=60, poll_interval_seconds=0.25)
        if terminal.status.value != "cancelled":
            raise RuntimeError("Function cancellation did not reach cancelled")
        rerun = cancelled.rerun()
        result = rerun.get(timeout_seconds=120, poll_interval_seconds=0.5)
        if result != 81:
            raise RuntimeError("rerun Function returned the wrong value")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "cancelled_task_id": cancelled.task_id,
                    "capability": "function.cancel-rerun",
                    "rerun_task_id": rerun.task_id,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
