"""Resolve one FunctionCall as the input to a second deployed Function.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique dependency app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("dependency app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function dependency")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_dependency import APP_NAME, app, double, square

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        upstream = square.spawn(6)
        downstream = double.spawn(upstream)
        result = downstream.get(timeout_seconds=120, poll_interval_seconds=0.5)
        if result != 72 or upstream.get(timeout_seconds=30) != 36:
            raise RuntimeError("Function dependency returned the wrong value")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.dependency",
                    "downstream_task_id": downstream.task_id,
                    "upstream_task_id": upstream.task_id,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
