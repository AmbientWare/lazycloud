"""Fan a list of inputs out across pooled containers with `.map()`.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario owns and publicly deletes one unique app.

This is the shape the pooling work exists for: many inputs, more than any one
container can hold at once, and far more than the ceiling the function declares.
Every part of that has to hold together — the answers come back in the order
they were asked for, containers serve several inputs each rather than one apiece,
the ceiling bounds how many are started, and the whole thing finishes in less
time than running the inputs one after another.
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
        raise RuntimeError("unique map app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function map")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_map import (
        APP_NAME,
        CONCURRENCY,
        HOLD_SECONDS,
        INPUTS,
        MAX_CONTAINERS,
        app,
        square_one,
    )

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)

        started = time.monotonic()
        answers = list(square_one.map(range(INPUTS)))
        elapsed = time.monotonic() - started

        if len(answers) != INPUTS:
            raise RuntimeError(f"map returned {len(answers)} answers for {INPUTS} inputs")
        failed = [index for index, answer in enumerate(answers) if answer is None]
        if failed:
            raise RuntimeError(f"map yielded no result for inputs {failed}")
        for index, answer in enumerate(answers):
            if answer["value"] != index * index:
                raise RuntimeError(
                    f"input {index} came back as {answer['value']}: map answered out of order"
                )

        containers = {str(answer["container_id"]) for answer in answers}
        # Paired with the container: pids are per namespace, so the same number
        # in two containers is two workers, not one.
        workers = {(str(answer["container_id"]), int(answer["pid"])) for answer in answers}
        if "" in containers:
            raise RuntimeError("an input was served by a container that did not name itself")
        if len(containers) > MAX_CONTAINERS:
            raise RuntimeError(
                f"{INPUTS} inputs used {len(containers)} containers, past the "
                f"max_containers={MAX_CONTAINERS} this function declared"
            )
        if len(containers) >= INPUTS:
            raise RuntimeError(
                "the fan-out started a container per input, which is the arrangement "
                "pooling replaced"
            )

        serial = INPUTS * HOLD_SECONDS
        if elapsed >= serial:
            raise RuntimeError(
                f"{INPUTS} inputs holding {HOLD_SECONDS}s each took {elapsed:.1f}s, "
                f"no better than one after another ({serial:.1f}s)"
            )
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.map",
                    "inputs": INPUTS,
                    "concurrency": CONCURRENCY,
                    "containers": len(containers),
                    "ceiling": MAX_CONTAINERS,
                    "workers": len(workers),
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
