"""Round-trip a custom Python value through one deployed Function.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates and publicly deletes one unique app.
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
        raise RuntimeError("unique serialization app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("serialization app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function serialization")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_serialization import APP_NAME, OpaqueNumber, app, opaque_square

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        result = opaque_square.remote(OpaqueNumber(9))
        if result != OpaqueNumber(81):
            raise RuntimeError("custom Python value did not round-trip")
        print(
            json.dumps(
                {"app": APP_NAME, "capability": "function.serialization", "result": result.value}
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
