"""Cancelling a running Function stops the work, not just the record.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.

A cancel that only writes `cancelled` on the row leaves the handler running to
completion: the caller is told their work stopped while its side effects carry
on and its container keeps being paid for. The handler here announces itself on
the far side of a hold, so the proof is public — a cancelled task whose logs
reach that announcement was never actually stopped.
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
TERMINAL_STATUSES = {"complete", "failed", "cancelled", "timeout", "expired"}


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique cancel-interrupt app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)


def _await_running(call: FunctionCall[str], *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = call.task.view().status.value
        if status == "running":
            return
        if status in TERMINAL_STATUSES:
            raise RuntimeError(f"Function reached {status} before cancellation")
        time.sleep(0.25)
    raise RuntimeError("Function did not reach running before cancellation")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function cancellation stops work")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_cancel_interrupt import AFTER_MARKER, APP_NAME, HOLD_SECONDS, app, two_phase

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        call = two_phase.spawn()
        _await_running(call, timeout_seconds=120)
        call.cancel()
        terminal = call.task.wait(timeout_seconds=60, poll_interval_seconds=0.25)
        if terminal.status.value != "cancelled":
            raise RuntimeError(f"cancel left the task {terminal.status.value}")

        # Past the point the handler would have finished had nothing stopped it.
        time.sleep(HOLD_SECONDS + 10.0)
        messages = [entry.message for entry in call.logs(limit=200)]
        if any(AFTER_MARKER in message for message in messages):
            raise RuntimeError(
                "the cancelled invocation ran to completion: its handler reached "
                f"{AFTER_MARKER!r} after the caller was told it was cancelled"
            )
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.cancel-stops-work",
                    "log_lines": len(messages),
                    "task_id": call.task_id,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
