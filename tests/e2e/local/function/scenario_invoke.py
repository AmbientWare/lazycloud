"""Invoke one deployed Function through the public SDK.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack. Creates one uniquely named app, verifies the returned value,
and deletes that app through the public resource API.
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
        raise RuntimeError("unique Function scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("Function scenario app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function invocation")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_invoke import APP_NAME, app, square

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        result = square.remote(7)
        if result != 49:
            raise RuntimeError("Function invocation returned the wrong value")
        print(json.dumps({"app": APP_NAME, "capability": "function.invoke", "result": result}))
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
