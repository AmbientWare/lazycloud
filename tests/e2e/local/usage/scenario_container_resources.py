"""Prove a bursting container bills what it used and a held disk bills its occupancy.

A request is a floor rather than a cap, so billing the reservation alone
undercounts a burst. Ephemeral disk carries no reservation floor at all: its
ceiling is oversubscribed by design, so only real occupancy is billable.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack. Creates one uniquely named app and deletes it through the
public resource API.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from lazycloud.cli.control import observability_client, resource_client
from lazycloud.clients.observability.control import ObservabilityControlClient
from shared.usage import UsageMetric
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent

# Occupancy is sampled on an interval, so billed byte-seconds are a lower bound
# on what was held. One interval's worth proves the reader reaches billing
# without depending on how many samples the hold happened to catch.
SAMPLE_INTERVAL_SECONDS = 5.0
USAGE_DEADLINE_SECONDS = 180.0

BILLED_METRICS = (
    UsageMetric.CpuSeconds,
    UsageMetric.CpuUsedCoreSeconds,
    UsageMetric.ContainerDurationMilliseconds,
    UsageMetric.ContainerDiskByteSeconds,
)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "container resource usage")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_container_resources import (
        APP_NAME,
        BURST_PROCESSES,
        BURST_SECONDS,
        HOLD_MIB,
        HOLD_SECONDS,
        REQUESTED_CORES,
        app,
        metered_workload,
    )

    workspace = profile.workspace
    started_at = datetime.now(UTC)
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        observed = metered_workload.remote(
            processes=BURST_PROCESSES,
            burst_seconds=BURST_SECONDS,
            hold_mib=HOLD_MIB,
            hold_seconds=HOLD_SECONDS,
        )
        app_id = _owned_app_id(APP_NAME, workspace)
        billed = _await_container_billing(
            observability_client(workspace=workspace, timeout_seconds=30),
            app_id,
            started_at,
        )
        _assert_cpu_bills_the_burst(billed, requested_cores=REQUESTED_CORES)
        _assert_disk_bills_occupancy(billed, observed)
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "usage.container_resource_usage",
                    "in_container": observed,
                    "billed": billed,
                },
                sort_keys=True,
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


def _await_container_billing(
    client: ObservabilityControlClient,
    app_id: str,
    started_at: datetime,
) -> dict[str, float]:
    """Total each billed container metric this app produced, once disk lands.

    Disk is the last of the four to appear because it needs a sampled window, so
    waiting on it means the cpu totals have settled too.
    """
    deadline = time.monotonic() + USAGE_DEADLINE_SECONDS
    totals: dict[str, float] = {}
    while time.monotonic() < deadline:
        totals = {
            metric.value: sum(
                record.quantity
                for record in client.usage_records(
                    metric=metric,
                    resource_type="container",
                    start=started_at,
                    limit=100,
                ).data
                if record.labels.get("app_id") == app_id
            )
            for metric in BILLED_METRICS
        }
        if totals[UsageMetric.ContainerDiskByteSeconds.value] > 0:
            return totals
        time.sleep(2)
    absent = sorted(name for name, total in totals.items() if total <= 0)
    raise RuntimeError(
        "container usage did not become publicly observable within "
        f"{USAGE_DEADLINE_SECONDS:.0f}s; absent metrics: {', '.join(absent) or 'none'}"
    )


def _assert_cpu_bills_the_burst(billed: dict[str, float], *, requested_cores: float) -> None:
    charged = billed[UsageMetric.CpuSeconds.value]
    measured = billed[UsageMetric.CpuUsedCoreSeconds.value]
    duration_seconds = billed[UsageMetric.ContainerDurationMilliseconds.value] / 1_000
    reservation = requested_cores * duration_seconds
    if charged < measured:
        raise RuntimeError(
            f"billed {charged:.3f} cpu core-seconds against {measured:.3f} measured; "
            "billing is below what the container consumed"
        )
    if charged <= reservation:
        raise RuntimeError(
            f"billed {charged:.3f} cpu core-seconds, no more than the {reservation:.3f} "
            f"reservation, despite consuming {measured:.3f}; the burst is not billed"
        )


def _assert_disk_bills_occupancy(billed: dict[str, float], observed: dict[str, float]) -> None:
    charged = billed[UsageMetric.ContainerDiskByteSeconds.value]
    held_bytes = observed["held_bytes"]
    held_seconds = observed["held_seconds"]
    if charged < held_bytes * SAMPLE_INTERVAL_SECONDS:
        raise RuntimeError(
            f"billed {charged:.0f} disk byte-seconds for {held_bytes:.0f} bytes held "
            f"{held_seconds:.0f}s; occupancy is not reaching billing"
        )
    duration_seconds = billed[UsageMetric.ContainerDurationMilliseconds.value] / 1_000
    if charged > held_bytes * duration_seconds:
        raise RuntimeError(
            f"billed {charged:.0f} disk byte-seconds, more than the {held_bytes:.0f} bytes "
            f"held could accrue over the container's {duration_seconds:.1f}s lifetime"
        )


def _owned_app_id(name: str, workspace: str) -> str:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        matches = [item for item in client.list_apps().data if item.name == name]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise RuntimeError("unique container usage scenario app resolved more than once")
        time.sleep(0.25)
    raise RuntimeError(f"app {name} was not publicly observable")


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique container usage scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("container usage scenario app remained active after deletion")


if __name__ == "__main__":
    raise SystemExit(main())
