"""Run several invocations at once inside one container.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack.

Wall time alone is a weak signal — a second container starting would also make
this finish quickly, and that is the arrangement concurrency was meant to avoid.
So the check is that the work ran concurrently *within* containers: distinct
worker processes, fewer containers than concurrent calls, and every invocation
seeing its own task id rather than a neighbour's.
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
        profile = require_live(argv, description=__doc__ or "Function concurrency")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_concurrency import APP_NAME, CONCURRENCY, HOLD_SECONDS, app, hold

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)

        # Warm one container first, so the measured window is the concurrency and
        # not the cold start that would dominate it.
        hold.remote(0)

        started = time.monotonic()
        calls = [hold.spawn(value) for value in range(1, CONCURRENCY + 1)]
        answers = [call.get(timeout_seconds=300) for call in calls]
        elapsed = time.monotonic() - started

        for index, answer in enumerate(answers, start=1):
            if answer["value"] != index * index:
                raise RuntimeError(f"invocation {index} returned {answer['value']}")

        task_ids = [str(answer["task_id"]) for answer in answers]
        if len(set(task_ids)) != len(task_ids) or "" in task_ids:
            raise RuntimeError(
                f"invocations did not each see their own task id: {task_ids}. "
                "A repeated id means one worker's identity leaked into another's call"
            )

        pids = {int(answer["pid"]) for answer in answers}
        containers = {str(answer["container_id"]) for answer in answers}
        if len(pids) < 2:
            raise RuntimeError(
                f"{CONCURRENCY} concurrent invocations ran in {len(pids)} process(es): "
                "nothing ran alongside anything else"
            )
        if len(containers) >= CONCURRENCY:
            raise RuntimeError(
                f"{CONCURRENCY} concurrent invocations used {len(containers)} containers: "
                "they were spread across containers rather than served inside them"
            )

        serial = CONCURRENCY * HOLD_SECONDS
        if elapsed >= serial:
            raise RuntimeError(
                f"{CONCURRENCY} invocations holding {HOLD_SECONDS}s each took {elapsed:.1f}s, "
                f"which is no better than running them one after another ({serial:.1f}s)"
            )

        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.concurrency",
                    "concurrency": CONCURRENCY,
                    "processes": len(pids),
                    "containers": len(containers),
                    "elapsed_seconds": round(elapsed, 2),
                    "serial_seconds": serial,
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
