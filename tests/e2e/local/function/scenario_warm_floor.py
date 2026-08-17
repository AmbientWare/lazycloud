"""A function's warm floor is held, not restarted.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.

A floor is a promise about latency: the containers are already up, so the call
that arrives does not pay for a start. Containers that idle out and are replaced
on the next tick satisfy any count taken once, and none of them is warm when it
matters. So the floor is read several times over a window longer than the
platform's own idle window, and the identities have to be the same ones each
time.
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
SETTLE_SECONDS = 90.0
HOLD_SECONDS = 45.0
SAMPLE_INTERVAL_SECONDS = 5.0


def _app_id(client: ResourceControlClient, name: str) -> str:
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique warm floor app resolved more than once")
    return matches[0].id if matches else ""


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=60)
    app_id = _app_id(client, name)
    if app_id:
        client.delete_app(app_id)


def _live_container_ids(client: ResourceControlClient, app_id: str) -> set[str]:
    live: set[str] = set()
    cursor: str | None = None
    while True:
        page = client.list_containers(app_id=app_id, limit=100, cursor=cursor)
        live.update(
            item.container.id
            for item in page.data
            if item.container.status.value in {"pending", "running"}
        )
        cursor = page.next or None
        if cursor is None:
            return live


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function warm floor")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_warm_floor import APP_NAME, FLOOR, app, floored

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        client = resource_client(workspace=workspace, timeout_seconds=60)
        app_id = _app_id(client, APP_NAME)
        if not app_id:
            raise RuntimeError("warm floor app was not registered")

        # Nothing is invoked: the floor has to be provisioned by the autoscaler
        # reading a stub that no call has arrived for.
        #
        # Settling is allowed to overshoot. Schedulers are replicated and each
        # reads the count before it acts, so two of them can provision the same
        # empty floor; the ceiling bounds that and the next scale-down reclaims
        # it. What is not allowed is staying above the floor, or arriving there
        # by restarting.
        deadline = time.monotonic() + SETTLE_SECONDS
        held: set[str] = set()
        peak = 0
        while time.monotonic() < deadline:
            held = _live_container_ids(client, app_id)
            peak = max(peak, len(held))
            if len(held) == FLOOR:
                break
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        if len(held) != FLOOR:
            raise RuntimeError(
                f"the floor never settled: {len(held)} container(s) up against a "
                f"declared floor of {FLOOR}, after {SETTLE_SECONDS:.0f}s with an empty backlog"
            )

        samples: list[int] = []
        settled = time.monotonic()
        while time.monotonic() - settled < HOLD_SECONDS:
            time.sleep(SAMPLE_INTERVAL_SECONDS)
            current = _live_container_ids(client, app_id)
            samples.append(len(current))
            if current != held:
                raise RuntimeError(
                    "the floor churned rather than being held: started with "
                    f"{sorted(held)} and now {sorted(current)}"
                )

        answer = floored.remote(7)
        if answer["value"] != 7:
            raise RuntimeError("held container answered incorrectly")
        if str(answer["container_id"]) not in held:
            raise RuntimeError(
                "the call was served by a container started for it, so the floor "
                "held containers nothing used"
            )
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.warm-floor",
                    "floor": FLOOR,
                    "held_container_ids": sorted(held),
                    "peak_while_settling": peak,
                    "samples": samples,
                    "served_by_held_container": True,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
