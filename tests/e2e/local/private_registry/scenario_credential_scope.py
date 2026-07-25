"""Reject a protected lookalike registry when no credentials are assigned.

Prerequisites: an authenticated public lazycloud profile and a prepared
protected LAZYCLOUD_E2E_PRIVATE_LOOKALIKE_IMAGE. Success is the public
deployment failure; any partially created uniquely named app is publicly
deleted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(
            argv,
            description=__doc__ or "private registry credential scope",
            required_env=("LAZYCLOUD_E2E_PRIVATE_LOOKALIKE_IMAGE",),
        )
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_scope import APP_NAME, app

    workspace = profile.workspace
    try:
        try:
            app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        except RuntimeError:
            print(json.dumps({"app": APP_NAME, "capability": "private-registry.credential-scope"}))
        else:
            raise RuntimeError("protected lookalike registry accepted unassigned credentials")
    finally:
        client = resource_client(workspace=workspace, timeout_seconds=30)
        matches = [item for item in client.list_apps(active=True).data if item.name == APP_NAME]
        if len(matches) > 1:
            raise RuntimeError("unique credential-scope app resolved more than once")
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == APP_NAME for item in client.list_apps(active=True).data):
            raise RuntimeError("credential-scope app remained active after deletion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
