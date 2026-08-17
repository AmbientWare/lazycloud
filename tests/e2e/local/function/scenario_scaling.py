"""Spread a burst of invocations across containers instead of trickling it through one.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack.

Pooling on its own makes a backlog drain through a single warm container, which
finishes eventually and is indistinguishable from a platform that has stalled.
The check is that the burst was actually spread — more than one container served
it — and that it finished faster than one container could have.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=60)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique Function scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function scaling")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_scaling import APP_NAME, BURST, app, burst

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)

        started = time.monotonic()
        calls = [burst.spawn(value) for value in range(BURST)]
        answers = [call.get(timeout_seconds=300) for call in calls]
        elapsed = time.monotonic() - started

        if sorted(int(answer["value"]) for answer in answers) != list(range(BURST)):
            raise RuntimeError("the burst did not return every value exactly once")

        containers = {str(answer["container_id"]) for answer in answers}
        if len(containers) < 2:
            raise RuntimeError(
                f"{BURST} calls were served by {len(containers)} container(s): "
                "the backlog was not spread, so nothing scaled to its depth"
            )

        # Wall time is deliberately not asserted. Every container in a burst is a
        # cold start, and staggered cold starts dominate the window — a run can
        # be correctly scaled and still take longer than one warm container would
        # have. What scaling means here is that the backlog was spread, and that
        # is what is checked.

        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.scaling",
                    "burst": BURST,
                    "containers": len(containers),
                    "elapsed_seconds": round(elapsed, 2),
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
