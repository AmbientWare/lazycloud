"""Run one public Function through the workspace's selected compute provider.

Prerequisite: an authenticated public LazyCloud profile targeting a healthy
prepared platform. The Function intentionally omits a placement override.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lazycloud.cli.control import compute_client, control_config, resource_client, task_client
from shared.http.compute_policy import WorkspaceComputeSummaryResponse
from shared.http.errors import HttpApiError, HttpTransportError

from .function_round_trip_workload import round_trip_app

SOURCE_ROOT = Path(__file__).resolve().parent
SKIP = 77
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


@dataclass(frozen=True, slots=True)
class PublicBaseline:
    instances: int
    ready: int
    pending: int
    degraded: int
    hourly_micros: int
    workload_count: int

    @classmethod
    def capture(cls, summary: WorkspaceComputeSummaryResponse) -> PublicBaseline:
        return cls(
            instances=summary.instances.total,
            ready=summary.instances.ready,
            pending=summary.instances.pending,
            degraded=summary.instances.degraded,
            hourly_micros=summary.cost.hourly_micros,
            workload_count=summary.workload_count,
        )


def _owned_app(name: str):
    matches = [
        app
        for app in resource_client(timeout_seconds=30).list_apps(active=True).data
        if app.name == name
    ]
    if len(matches) > 1:
        raise RuntimeError("the exact Function app resolved more than once")
    return matches[0] if matches else None


def _delete_owned_app(name: str, *, timeout_seconds: float = 60) -> None:
    app = _owned_app(name)
    if app is not None:
        resource_client(timeout_seconds=30).delete_app(app.id)
    deadline = time.monotonic() + timeout_seconds
    while _owned_app(name) is not None:
        if time.monotonic() >= deadline:
            raise RuntimeError("the Function app remained active after public deletion")
        time.sleep(1)


def _wait_for_baseline(expected: PublicBaseline, *, timeout_seconds: float = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    observed = PublicBaseline.capture(compute_client(timeout_seconds=30).summary())
    while observed != expected:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "public compute did not return to its pre-Function baseline: "
                f"expected={expected}, observed={observed}"
            )
        time.sleep(2)
        observed = PublicBaseline.capture(compute_client(timeout_seconds=30).summary())


def _wait_for_marker(task_id: str, marker: str, *, timeout_seconds: float = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    output = task_client(timeout_seconds=30).output(task_id, limit=250)
    while marker not in output:
        if time.monotonic() >= deadline:
            raise RuntimeError("the public task logs did not contain the Function marker")
        time.sleep(1)
        output = task_client(timeout_seconds=30).output(task_id, limit=250)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if _RUN_ID.fullmatch(args.run_id) is None:
        parser.error("--run-id must be lowercase letters, digits, or dashes")

    app_name = f"e2e_function_{args.run_id.replace('-', '_')}"
    marker = f"function-round-trip:{args.run_id}"
    try:
        control_config(timeout_seconds=30)
        compute_client(timeout_seconds=30).summary()
    except HttpTransportError as exc:
        print(f"Function E2E prerequisite unavailable: {exc}", file=sys.stderr)
        return SKIP
    except HttpApiError as exc:
        if exc.status_code != 401:
            raise
        print(
            "Function E2E prerequisite unavailable: public authentication failed", file=sys.stderr
        )
        return SKIP

    _delete_owned_app(app_name)
    baseline = PublicBaseline.capture(compute_client(timeout_seconds=30).summary())
    app, function = round_trip_app(app_name)
    task_id = ""
    try:
        app.deploy(source_root=SOURCE_ROOT)
        call = function.spawn(marker, 21)
        task_id = call.task_id
        result = call.get(timeout_seconds=600, poll_interval_seconds=1)
        if result != {"marker": marker, "doubled": 42}:
            raise RuntimeError("the public Function returned the wrong result")
        _wait_for_marker(task_id, marker)
    finally:
        _delete_owned_app(app_name)
        _wait_for_baseline(baseline)

    print(
        json.dumps(
            {
                "app": app_name,
                "capability": "function.round_trip",
                "marker": marker,
                "provider": compute_client(timeout_seconds=30).summary().policy.default_placement,
                "result": 42,
                "task_id": task_id,
            },
            default=str,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
