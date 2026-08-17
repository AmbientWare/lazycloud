"""Invoke one deployed Function repeatedly and prove the container is reused.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack.

This is the scenario the pooling work exists for, and it is written so that the
old behaviour fails it. Every invocation returning the right answer proves
nothing — one container per call did that too. What is checked is that the
invocations were served by fewer containers than there were calls, and that
`on_start` ran once per container rather than once per call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent
INVOCATIONS = 5


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=60)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique Function scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function pooling")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_pooling import APP_NAME, app, identify

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)

        containers: list[str] = []
        answers: list[dict[str, str | int]] = []
        for value in range(INVOCATIONS):
            answer = identify.remote(value)
            if answer["value"] != value * value:
                raise RuntimeError("pooled Function returned the wrong value")
            container_id = str(answer["container_id"])
            if not container_id:
                raise RuntimeError("pooled Function did not report its container")
            containers.append(container_id)
            answers.append(answer)

        distinct = sorted(set(containers))
        if len(distinct) >= INVOCATIONS:
            raise RuntimeError(
                f"{INVOCATIONS} invocations ran on {len(distinct)} containers: "
                "nothing was reused, so the container is not staying warm"
            )

        # Every call reports what its own process has done. One start means
        # `on_start` ran once for that interpreter however many calls it served —
        # the property the hook was documented to have and never had. Checked for
        # equality because both directions are real failures: more means startup
        # is still being paid per invocation, fewer means it stopped running.
        starts = {int(answer["starts"]) for answer in answers}
        if starts != {1}:
            raise RuntimeError(
                f"on_start run counts seen across invocations were {sorted(starts)}, "
                "expected exactly one run per serving process"
            )
        # And the same process really did serve several calls, rather than each
        # call getting a fresh interpreter that ran startup once on its way in.
        if max(int(answer["calls"]) for answer in answers) < 2:
            raise RuntimeError("no process served more than one call, so nothing was reused")

        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.pooling",
                    "invocations": INVOCATIONS,
                    "containers": len(distinct),
                    "calls_per_process": max(int(answer["calls"]) for answer in answers),
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
