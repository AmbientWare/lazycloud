"""Measure four cold and warm scheduled executions through the public task API.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from lazycloud.function_results import decode_function_result
from lazycloud.session.task import TaskClient
from shared.tasks import TaskStatus, is_terminal_task_status
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps().data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique schedule app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps().data):
        raise RuntimeError("schedule app remained active after deletion")


def _await_scheduled_tasks(stubs: dict[str, str], *, timeout_seconds: float) -> None:
    client = TaskClient()
    deadline = time.monotonic() + timeout_seconds
    seen: set[str] = set()
    occurrences: set[tuple[str, str]] = set()
    counts = dict.fromkeys(stubs, 0)
    while time.monotonic() < deadline:
        for task in client.list(stub_ids=tuple(stubs), limit=100):
            if task.id in seen or not is_terminal_task_status(task.status):
                continue
            assert task.status is TaskStatus.Complete, task
            assert task.stub_id is not None
            assert decode_function_result(client.result(task.id).value) == stubs[task.stub_id]
            assert task.started_at is not None and task.finished_at is not None
            scheduled_at = task.created_at.replace(second=0, microsecond=0)
            occurrence = (task.stub_id, scheduled_at.isoformat())
            assert occurrence not in occurrences, "duplicate scheduled task for one minute"
            occurrences.add(occurrence)
            seen.add(task.id)
            counts[task.stub_id] += 1
            print(
                json.dumps(
                    {
                        "workload": stubs[task.stub_id],
                        "sample": counts[task.stub_id],
                        "task_id": task.id,
                        "container_id": task.container_id,
                        "admission_ms": (task.created_at - scheduled_at).total_seconds() * 1000,
                        "start_ms": (task.started_at - scheduled_at).total_seconds() * 1000,
                        "complete_ms": (task.finished_at - scheduled_at).total_seconds() * 1000,
                    }
                ),
                flush=True,
            )
        if all(count >= 4 for count in counts.values()):
            return
        time.sleep(1)
    raise RuntimeError(f"scheduled Functions did not complete before timeout: {counts}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function schedule")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_schedule import APP_NAME, app, scheduled_marker, warm_marker

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        if not scheduled_marker.stub_id or not warm_marker.stub_id:
            raise RuntimeError("scheduled Function deployment omitted its stub ID")
        print(json.dumps({"app": APP_NAME, "capability": "function.schedule"}), flush=True)
        _await_scheduled_tasks(
            {scheduled_marker.stub_id: "scheduled-marker", warm_marker.stub_id: "warm-marker"},
            timeout_seconds=300,
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
