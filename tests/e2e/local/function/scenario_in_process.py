"""Serve many invocations at once in one interpreter, without them mixing.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack.

`concurrency` on its own is served by a process per slot, which is correct and
cannot share a GPU: four processes load four copies of a model onto a card that
holds one. `in_process=True` puts the slots in one interpreter instead, and the
whole risk of that is what the interpreter shares. Two things are checked, and
neither alone is enough:

  * Sharing happened. Every invocation was served by the same process and the
    same object loaded by `on_start` — the property a model in VRAM rests on.
    Without it this is the process mode wearing a different name.
  * Nothing crossed. Each invocation reported the task id its own caller holds,
    not a neighbour's. That is the failure the environment variable this
    replaced would produce, and it is silent: the call still returns, the result
    is still right, and only the attribution is somebody else's.

  * Something overlapped. Read from the invocations' own clock rather than the
    wall clock, because fifty spawns are fifty round trips from here and that
    submission cost dominates a window this short. Both checks above would pass
    a run that served everything one at a time.
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


def _peak_overlap(windows: list[tuple[float, float]]) -> int:
    """The most invocations that were running at the same instant."""

    edges = sorted(
        [(start, 1) for start, _ in windows] + [(end, -1) for _, end in windows],
        # A window ending exactly where another begins is not an overlap, so
        # closes are applied before opens at the same instant.
        key=lambda edge: (edge[0], edge[1]),
    )
    peak = 0
    running = 0
    for _, delta in edges:
        running += delta
        peak = max(peak, running)
    return peak


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function in-process concurrency")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_in_process import (
        APP_NAME,
        CONCURRENCY,
        INVOCATIONS,
        app,
        shared,
    )

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)

        # Warm one container first. The measured window is meant to be the
        # concurrency, and a cold start plus an `on_start` would dominate it.
        shared.remote(0)

        started = time.monotonic()
        calls = [shared.spawn(value) for value in range(1, INVOCATIONS + 1)]
        answers = [call.get(timeout_seconds=600) for call in calls]
        elapsed = time.monotonic() - started

        for index, answer in enumerate(answers, start=1):
            if answer["value"] != index * index:
                raise RuntimeError(f"invocation {index} returned {answer['value']}")

        crossed = [
            (call.task_id, str(answer["task_id"]))
            for call, answer in zip(calls, answers, strict=True)
            if str(answer["task_id"]) != call.task_id
        ]
        if crossed:
            raise RuntimeError(
                f"{len(crossed)} of {INVOCATIONS} invocations reported a task id that is not "
                f"their own, first {crossed[0]}: an invocation's identity reached another's "
                "handler, so anything it spawns or stores is attributed to the wrong call"
            )

        pids = {int(answer["pid"]) for answer in answers}
        threads = {int(answer["thread"]) for answer in answers}
        model_ids = {int(answer["model_id"]) for answer in answers}
        loaded_by = {int(answer["loaded_by"]) for answer in answers}
        if len(model_ids) != 1 or 0 in model_ids:
            raise RuntimeError(
                f"invocations were served by {len(model_ids)} distinct loaded objects: "
                "nothing was shared, so a model would have been loaded once per slot"
            )
        if loaded_by != pids:
            raise RuntimeError(
                f"objects were loaded by {loaded_by} but served from {pids}: the slots did "
                "not share the interpreter that ran on_start"
            )
        if len(threads) < 2:
            raise RuntimeError(
                f"{INVOCATIONS} invocations ran on {len(threads)} thread(s): "
                "nothing ran alongside anything else"
            )

        # Read from the invocations rather than from the wall clock. Fifty
        # spawns are fifty HTTP round trips from here, and that submission cost
        # dominates a window this short — a correctly concurrent run can take
        # longer than the serial arithmetic and still never have run anything
        # alone. Overlap is the property, so overlap is what is measured.
        peak = _peak_overlap(
            [(float(answer["started_at"]), float(answer["finished_at"])) for answer in answers]
        )
        if peak < 2:
            raise RuntimeError(
                f"{INVOCATIONS} invocations never overlapped (peak {peak}): they were served "
                "one at a time, so nothing was concurrent inside the interpreter"
            )

        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.in-process-concurrency",
                    "concurrency": CONCURRENCY,
                    "invocations": INVOCATIONS,
                    "processes": len(pids),
                    "threads": len(threads),
                    "shared_objects": len(model_ids),
                    "peak_overlap": peak,
                    "elapsed_seconds": round(elapsed, 2),
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
