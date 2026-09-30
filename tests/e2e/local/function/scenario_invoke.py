"""Deploy, invoke, redeploy, pause, resume and delete a Function app through the SDK.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack. Creates one uniquely named app, verifies the returned value,
and deletes that app through the public resource API. Set LAZYCLOUD_E2E_MACHINE
to the joined local machine's name when using customer compute.
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
    matches = [item for item in client.list_apps().data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique Function scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps().data):
        raise RuntimeError("Function scenario app remained after deletion")


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
        client = resource_client(workspace=workspace, timeout_seconds=60)
        deployed_app = next(item for item in client.list_apps().data if item.name == APP_NAME)
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        versions = client.list_deployments(app_id=deployed_app.id).data
        if sorted(item.version for item in versions) != [1, 2]:
            raise RuntimeError("redeployment did not retain both versions")
        latest = max(versions, key=lambda item: item.version)
        if client.stop_deployment(latest.id).active:
            raise RuntimeError("deployment remained active after stopping")
        if not client.start_deployment(latest.id).active:
            raise RuntimeError("deployment did not restart")
        if client.pause_app(deployed_app.id).active:
            raise RuntimeError("app remained active after pausing")
        if client.list_deployments(app_id=deployed_app.id, active=True).data:
            raise RuntimeError("paused app retained active deployments")
        if not client.resume_app(deployed_app.id).active or square.remote(8) != 64:
            raise RuntimeError("resumed app did not serve the next invocation")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "deployment.lifecycle",
                    "versions": 2,
                    "results": [49, 64],
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
