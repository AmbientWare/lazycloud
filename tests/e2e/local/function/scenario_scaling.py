"""Spread a burst of invocations across containers, and no further than the ceiling.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack.

Pooling on its own makes a backlog drain through a single warm container, which
finishes eventually and is indistinguishable from a platform that has stalled.
Scaling without a bound is the opposite failure and the more expensive one: a
burst that starts a container per call is the arrangement pooling replaced, and
`max_containers` is the number a customer sized their bill against.

Both sides are read from the same burst of exactly `max_containers` calls, each
holding its container long enough that a spread is the only way to serve them
concurrently. The spread is read from the answers, because a container that
served a call demonstrably existed. The ceiling cannot be read from them — six
answers name at most six containers however many were started — so it is read
from the containers the workspace records for the app, which is where the
overshoot this checks for was visible in the first place.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from lazycloud.clients.resource.control import ResourceControlClient
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent
SETTLE_CYCLES = 4
SETTLE_INTERVAL_SECONDS = 1.0


def _app_id(client: ResourceControlClient, name: str) -> str:
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) != 1:
        raise RuntimeError("unique Function scenario app resolved more than once")
    return matches[0].id


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=60)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique Function scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)


def _started_container_ids(client: ResourceControlClient, app_id: str) -> set[str]:
    """Every container the workspace records for this app, in any state.

    No status filter: a container that started and then exited was still
    capacity somebody paid for, and a ceiling that only holds for containers
    still running is not a ceiling.
    """

    seen: set[str] = set()
    cursor: str | None = None
    while True:
        page = client.list_containers(app_id=app_id, limit=100, cursor=cursor)
        seen.update(item.container.id for item in page.data)
        if not page.next:
            return seen
        cursor = page.next


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function scaling")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_scaling import APP_NAME, BURST, app, burst

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        client = resource_client(workspace=workspace, timeout_seconds=60)
        app_id = _app_id(client, APP_NAME)

        started = time.monotonic()
        calls = [burst.spawn(value) for value in range(BURST)]
        answers = [call.get(timeout_seconds=300) for call in calls]
        elapsed = time.monotonic() - started

        if sorted(int(answer["value"]) for answer in answers) != list(range(BURST)):
            raise RuntimeError("the burst did not return every value exactly once")

        serving = {str(answer["container_id"]) for answer in answers}
        if len(serving) < 2:
            raise RuntimeError(
                f"{BURST} calls were served by {len(serving)} container(s): "
                "the backlog was not spread, so nothing scaled to its depth"
            )

        # Sampled across several scheduler ticks rather than once. The last
        # result landing does not prove the last start did, and one sample taken
        # a moment early reads an overshoot as compliance. Every cycle is
        # printed so a run that is drifting says so while it is still running.
        started_containers: set[str] = set()
        for cycle in range(SETTLE_CYCLES):
            started_containers |= _started_container_ids(client, app_id)
            print(
                json.dumps(
                    {
                        "cycle": cycle,
                        "started_containers": len(started_containers),
                        "serving_containers": len(serving),
                        "ceiling": BURST,
                    }
                ),
                flush=True,
            )
            time.sleep(SETTLE_INTERVAL_SECONDS)

        if len(started_containers) > BURST:
            raise RuntimeError(
                f"{BURST} calls started {len(started_containers)} containers, past the "
                f"max_containers={BURST} this function declared: the ceiling did not bound"
            )

        # Wall time is deliberately not asserted. Every container in a burst is a
        # cold start, and staggered cold starts dominate the window — a run can
        # be correctly scaled and still take longer than one warm container would
        # have. What scaling means here is that the backlog was spread and that
        # the spread stopped at the ceiling, and that is what is checked.

        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.scaling",
                    "burst": BURST,
                    "serving_containers": len(serving),
                    "started_containers": len(started_containers),
                    "ceiling": BURST,
                    "elapsed_seconds": round(elapsed, 2),
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
