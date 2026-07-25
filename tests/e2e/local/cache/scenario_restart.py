"""Invoke one built Function before and after a cache-server restart.

This is the only public cache E2E retained: it proves workload continuity at
the process boundary without inspecting private cache, worker, database, Redis,
or object-store state. Requires the live local environment variables documented
by tests/e2e through an authenticated public lazycloud profile. The uniquely
named app is publicly deleted.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import (
    LivePrerequisiteError,
    blocked,
    require_live,
    run_text_process,
)

ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = Path(__file__).resolve().parent


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "cache restart")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_restart import APP_NAME, BUILD_MARKER, app, cache_restart_probe

    workspace = profile.workspace
    try:
        app.deploy(
            workspace=workspace,
            source_root=SOURCE_ROOT,
            env={
                "LAZYCLOUD_E2E_APP": APP_NAME,
                "LAZYCLOUD_E2E_CACHE_MARKER": BUILD_MARKER,
            },
        )
        if cache_restart_probe.remote(BUILD_MARKER) != BUILD_MARKER:
            raise RuntimeError("cache restart probe failed before restart")
        run_text_process(
            ("docker", "compose", "restart", "cache-server"),
            cwd=ROOT,
            environment=os.environ,
            timeout=120,
        )
        if cache_restart_probe.remote(BUILD_MARKER) != BUILD_MARKER:
            raise RuntimeError("cache restart probe failed after restart")
        print(json.dumps({"app": APP_NAME, "capability": "cache.restart-continuity"}))
    finally:
        client = resource_client(workspace=workspace, timeout_seconds=30)
        matches = [item for item in client.list_apps(active=True).data if item.name == APP_NAME]
        if len(matches) > 1:
            raise RuntimeError("unique cache restart app resolved more than once")
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == APP_NAME for item in client.list_apps(active=True).data):
            raise RuntimeError("cache restart app remained active after deletion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
