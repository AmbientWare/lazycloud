"""Exercise light traffic with 1, 5 and 10 clients, then 5 total requests/second.

Requires an authenticated local profile and at least 2 free CPU and 3 GiB.
One account mixes short functions, one-second jobs, HTTP and sandbox commands.
The fixed-rate phase lasts one minute. Up to five containers fit within the
Free plan's no-card container allowance. The account's actual billing terms
still apply; this scenario does not establish Free-plan enforcement or peak
throughput. It also checks a nested SDK call through the agent tunnel.
Created apps and containers are removed through their public owners.
Use --burst-clients 100 to add a bounded burst against the same container limits.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from lazycloud.abstractions.sandbox import SandboxInstance
from lazycloud.cli.control import resource_client
from lazycloud.clients.pod.control import PodControlClient
from shared.http.errors import HttpApiError
from shared.tasks import TaskStatus
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live


@dataclass(frozen=True)
class Sample:
    clients: int
    kind: str
    seconds: float
    error: str = ""
    offered_rps: int = 0
    client_queue_seconds: float = 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--burst-clients", type=int, default=10)
    options, live_args = parser.parse_known_args(argv)
    if not 10 <= options.burst_clients <= 100:
        parser.error("--burst-clients must be between 10 and 100")
    try:
        profile = require_live(live_args, description=__doc__ or "Mixed workload scaling")
        hostname = urlsplit(profile.resolved_endpoint()).hostname or ""
        if hostname not in {"127.0.0.1", "localhost"} and not hostname.endswith(".localhost"):
            raise LivePrerequisiteError("this scenario requires a local deployment")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    os.environ["LAZYCLOUD_SCALING_APP"] = f"scaling_{uuid4().hex[:12]}"
    from .workload import APP_NAME, app, echo, interactive, nested, sandbox

    client = resource_client(workspace=profile.workspace, timeout_seconds=60)
    instance: SandboxInstance | None = None
    app_id = ""
    samples: list[Sample] = []
    try:
        deployed = app.deploy(workspace=profile.workspace, source_root=Path(__file__).parent)
        app_id = deployed.resources[0].app_id
        if not app_id:
            raise RuntimeError("deployment omitted its app identity")
        started = time.monotonic()
        instance = sandbox.create(timeout_seconds=120)
        print(json.dumps({"cold_sandbox_seconds": time.monotonic() - started}), flush=True)
        started = time.monotonic()
        if nested.remote(917) != 917:
            raise RuntimeError("nested function result did not match")
        print(json.dumps({"cold_nested_seconds": time.monotonic() - started}), flush=True)
        started = time.monotonic()
        if echo.target("deployed").request(918).json() != {"sequence": 918}:
            raise RuntimeError("endpoint warmup result did not match")
        print(json.dumps({"cold_endpoint_seconds": time.monotonic() - started}), flush=True)
        upstream = interactive.spawn(919)
        if interactive.spawn(upstream).get(timeout_seconds=30) != 919:
            raise RuntimeError("dependent function result did not match")
        cancelled = interactive.spawn(920, 10.0)
        neighbour = interactive.spawn(921, 3.0)
        deadline = time.monotonic() + 15
        while True:
            views = [call.task.view() for call in (cancelled, neighbour)]
            print(
                json.dumps(
                    {
                        "cancellation_probe": [
                            {
                                "task": view.id,
                                "status": view.status.value,
                                "container": view.container_id,
                                "container_status": (
                                    view.container.status.value if view.container else None
                                ),
                                "log_records": len(call.logs(limit=5)),
                            }
                            for call, view in zip((cancelled, neighbour), views, strict=True)
                        ]
                    }
                ),
                flush=True,
            )
            if all(view.status is TaskStatus.Running for view in views):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("calls did not start before cancellation")
            if any(view.status in {TaskStatus.Failed, TaskStatus.Complete} for view in views):
                raise RuntimeError("call finished before cancellation could be exercised")
            time.sleep(0.2)
        neighbour_container = views[1].container_id
        cancelled.cancel()
        if cancelled.task.view().status is not TaskStatus.Cancelled:
            raise RuntimeError("cancellation did not commit its terminal state")
        if neighbour.get(timeout_seconds=15) != 921:
            raise RuntimeError("cancellation disrupted the neighbouring invocation")
        if neighbour.task.view().container_id != neighbour_container:
            raise RuntimeError("cancellation restarted the neighbouring invocation")
        if cancelled.rerun().get(timeout_seconds=30) != 920:
            raise RuntimeError("cancelled invocation could not run again")
        print(json.dumps({"cancellation_and_rerun": "passed"}), flush=True)
        print(json.dumps({"ready": app_id, "nested_call": "passed"}), flush=True)
        retained = instance.process.exec("printf", "%s", "retained-before-load")
        if retained.result(timeout=10).stdout != "retained-before-load":
            raise RuntimeError("sandbox retention command did not finish")

        def request(
            index: int, clients: int, *, offered_at: float | None = None, offered_rps: int = 0
        ) -> Sample:
            kind = ("interactive", "http", "job", "command")[index % 4]
            entered = time.monotonic()
            started = entered if offered_at is None else offered_at
            error = ""
            try:
                if kind == "http":
                    response = echo.target("deployed").request(index)
                    if response.status_code != 200:
                        raise RuntimeError(f"endpoint failed with HTTP {response.status_code}")
                    if response.json() != {"sequence": index}:
                        raise RuntimeError("endpoint returned an incorrect sequence")
                elif kind == "command":
                    if instance is None:
                        raise RuntimeError("sandbox is unavailable")
                    result = instance.run(["printf", "%s", str(index)], timeout_seconds=30)
                    if result.exit_code or result.stdout != str(index):
                        raise RuntimeError("sandbox command result did not match")
                elif interactive.remote(index, 1.0 if kind == "job" else 0.02) != index:
                    raise RuntimeError("function returned another request's result")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            sample = Sample(
                clients,
                kind,
                time.monotonic() - started,
                error,
                offered_rps,
                max(0.0, entered - started),
            )
            print(json.dumps(asdict(sample)), flush=True)
            return sample

        for clients in dict.fromkeys((1, 5, 10, options.burst_clients)):
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=clients) as executor:
                pending = [executor.submit(request, index, clients) for index in range(clients * 4)]
                samples.extend(future.result() for future in as_completed(pending))
            elapsed = time.monotonic() - started
            phase = [sample for sample in samples if sample.clients == clients]
            print(
                json.dumps(
                    {
                        "clients": clients,
                        "requests": len(phase),
                        "requests_per_second": len(phase) / elapsed,
                        "errors": sum(bool(sample.error) for sample in phase),
                    }
                ),
                flush=True,
            )
            if any(sample.error for sample in phase):
                raise RuntimeError(f"mixed workload phase at {clients} clients failed")

        offered_rps = 5
        duration_seconds = 60
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=10) as executor:
            pending: list[Future[Sample]] = []
            for index in range(offered_rps * duration_seconds):
                offered_at = started + index / offered_rps
                time.sleep(max(0.0, offered_at - time.monotonic()))
                pending.append(
                    executor.submit(
                        request, index, 10, offered_at=offered_at, offered_rps=offered_rps
                    )
                )
            phase = [future.result() for future in as_completed(pending)]
        samples.extend(phase)
        print(
            json.dumps(
                {
                    "offered_rps": offered_rps,
                    "requests": len(phase),
                    "drain_seconds": max(0.0, time.monotonic() - started - duration_seconds),
                    "max_client_queue_seconds": max(
                        sample.client_queue_seconds for sample in phase
                    ),
                    "errors": sum(bool(sample.error) for sample in phase),
                }
            ),
            flush=True,
        )
        if any(sample.error for sample in phase):
            raise RuntimeError("controlled-arrival mixed workload phase failed")
        if any(sample.client_queue_seconds >= 1 / offered_rps for sample in phase):
            raise RuntimeError("load generator missed its fixed arrival rate")

        reconnected = PodControlClient.from_endpoint(
            profile.resolved_endpoint(), token=profile.token, workspace=profile.workspace
        )
        result = reconnected.sandbox_result(instance.container_id, retained.process_id, 0)
        if result.running or result.exit_code or result.stdout != "retained-before-load":
            raise RuntimeError("sandbox result was lost after concurrent commands")
        print(json.dumps({"retained_result_after_reconnect": "passed"}), flush=True)
    finally:
        try:
            if instance is not None and not instance.terminate():
                raise RuntimeError("sandbox termination failed")
        finally:
            if not app_id:
                matches = [
                    item for item in client.list_apps(active=True).data if item.name == APP_NAME
                ]
                if len(matches) > 1:
                    raise RuntimeError("scenario app identity is ambiguous")
                app_id = matches[0].id if matches else ""
            if app_id:
                client.delete_app(app_id)
                try:
                    client.app(app_id)
                except HttpApiError as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    raise RuntimeError("deleted app is still available")
                print(json.dumps({"deleted_app": app_id}), flush=True)
        os.environ.pop("LAZYCLOUD_SCALING_APP", None)

    groups: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        groups[f"{sample.clients}:{sample.offered_rps}:{sample.kind}"].append(sample.seconds)
    for group, values in groups.items():
        values.sort()
        print(
            json.dumps(
                {
                    "group": group,
                    "requests": len(values),
                    "p50_seconds": values[len(values) // 2],
                    "p95_seconds": values[min(len(values) - 1, int(len(values) * 0.95))],
                    "p99_seconds": values[min(len(values) - 1, int(len(values) * 0.99))],
                    "under_five_seconds": sum(value < 5 for value in values),
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
