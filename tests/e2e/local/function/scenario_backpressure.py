"""Refuse a fan-out that outruns what a Function will queue.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack.

The refusal has to reach the caller as a refusal. Accepting the call and then
never getting to it is the failure this prevents: indistinguishable, from
outside, from a function that is merely slow.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.abstractions.function import FunctionOperationError
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
        profile = require_live(argv, description=__doc__ or "Function backpressure")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_backpressure import APP_NAME, MAX_PENDING, app, occupy

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)

        accepted = [occupy.spawn(value) for value in range(MAX_PENDING)]

        refusal = ""
        for value in range(MAX_PENDING, MAX_PENDING * 3):
            try:
                accepted.append(occupy.spawn(value))
            except FunctionOperationError as exc:
                refusal = str(exc)
                break
        if not refusal:
            raise RuntimeError(
                f"spawning {MAX_PENDING * 3} calls against a limit of {MAX_PENDING} "
                "was never refused"
            )
        if "in flight" not in refusal:
            raise RuntimeError(f"refusal did not say why it refused: {refusal}")
        if len(accepted) < MAX_PENDING:
            raise RuntimeError(
                f"only {len(accepted)} calls were accepted before the limit of {MAX_PENDING}"
            )

        # Drained before the app is deleted. Deleting it while calls are still
        # running asks the workers to confirm a shutdown they are mid-invocation
        # for, and the scenario would fail in teardown having proved its point.
        for call in accepted:
            call.result(wait=True, timeout_seconds=180)

        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "function.backpressure",
                    "max_pending_tasks": MAX_PENDING,
                    "accepted": len(accepted),
                    "refused_with": refusal[:80],
                }
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
