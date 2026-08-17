"""Cancel one running Function task, rerun it, and leave its neighbour alone.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.

Cancellation is a statement about one invocation. A pooled container serves
several at once, so honouring a cancel means stopping a container that other
callers are still waiting on: what happens to them is the contract this proves.
Their call is still wanted, so it may be re-run elsewhere, but it may never come
back cancelled — nobody asked for that, and cancelled is terminal.
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
HOLD_SECONDS = 25.0
TERMINAL_STATUSES = {"complete", "failed", "cancelled", "timeout", "expired"}


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique cancel/rerun app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("cancel/rerun app remained active after deletion")


def _await_co_resident(
    calls: Sequence[FunctionCall[int]],
    *,
    timeout_seconds: float,
) -> str:
    """Block until every call is running, and report the container serving them.

    A cancel that stopped a container nobody else was using would prove nothing
    about neighbours, so the container is read back rather than assumed.
    """

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        views = [call.task.view() for call in calls]
        statuses = [view.status.value for view in views]
        terminal = [status for status in statuses if status in TERMINAL_STATUSES]
        if terminal:
            raise RuntimeError(f"a call reached {terminal[0]} before cancellation")
        containers = {view.container_id or "" for view in views}
        if all(status == "running" for status in statuses) and len(containers) == 1:
            serving = containers.pop()
            if serving:
                return serving
        time.sleep(0.25)
    raise RuntimeError("calls did not reach running together on one container")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function cancel and rerun")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_cancel_rerun import APP_NAME, app, delayed_square

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        cancelled = delayed_square.spawn(9, delay_seconds=HOLD_SECONDS)
        neighbour = delayed_square.spawn(6, delay_seconds=HOLD_SECONDS)
        serving = _await_co_resident([cancelled, neighbour], timeout_seconds=120)

        cancelled.cancel()
        terminal = cancelled.task.wait(timeout_seconds=60, poll_interval_seconds=0.25)
        if terminal.status.value != "cancelled":
            raise RuntimeError("Function cancellation did not reach cancelled")

        neighbour_result = neighbour.get(timeout_seconds=300, poll_interval_seconds=0.5)
        if neighbour_result != 36:
            raise RuntimeError(f"neighbouring invocation returned {neighbour_result}")
        # Where it finished is what separates "survived the stop" from "was never
        # interrupted": the container it shared with the cancelled call is gone,
        # so an answer from that same container would mean the cancel never
        # reached the work it was supposed to stop.
        neighbour_container = neighbour.task.view().container_id or ""
        if neighbour_container == serving:
            raise RuntimeError(
                "the neighbouring invocation finished on the container the cancel "
                "stopped, so the cancelled handler was never interrupted"
            )

        rerun = cancelled.rerun()
        result = rerun.get(timeout_seconds=300, poll_interval_seconds=0.5)
        if result != 81:
            raise RuntimeError("rerun Function returned the wrong value")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "cancelled_task_id": cancelled.task_id,
                    "capability": "function.cancel-rerun",
                    "neighbour_task_id": neighbour.task_id,
                    "rerun_task_id": rerun.task_id,
                    "shared_container_id": serving,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
