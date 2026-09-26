from dataclasses import replace
from datetime import timedelta

from compute.demand_forecast import DemandSample, forecast_demand
from compute.fleet_resources import Capacity
from compute.scheduled_forecast import backlog_container_demand, scheduled_container_demand
from database.repositories.fleet_demand import FleetBacklogRow, FleetDemandRow
from shared.timestamps import utc_now


def test_scheduled_invocations_share_concurrency_and_warm_allocations() -> None:
    now = utc_now()
    base = FleetDemandRow(
        observed_at=now,
        preemptible=False,
        gpu_types=(),
        cpu_millicores=1000,
        memory_mib=1024,
        gpu_count=0,
        count=1,
        pending_count=0,
        workload_id="function",
        concurrency=2,
        keep_warm_seconds=60,
        max_containers=2,
        duration_seconds=30,
    )
    rows = [replace(base, observed_at=now + timedelta(seconds=at)) for at in (30, 40, 50, 60, 180)]
    projected = scheduled_container_demand(rows, now=now, until=now + timedelta(seconds=300))
    forecast = forecast_demand(
        (),
        now=now,
        resume_seconds=120,
        provision_seconds=300,
        scheduled=[
            DemandSample(
                row.observed_at, Capacity(1000, 1024), duration_seconds=row.duration_seconds
            )
            for row in projected
        ],
    )
    assert len(projected) == 3
    assert forecast.scheduled_warm == Capacity(2000, 2048)
    assert forecast.scheduled_total == Capacity(2000, 2048)
    warm = scheduled_container_demand(
        [replace(row, keep_warm_seconds=-1, existing_containers=2) for row in rows],
        now=now,
        until=now + timedelta(seconds=300),
    )
    assert warm == []


def test_backlog_deducts_existing_container_commitments_and_respects_workload_ceiling() -> None:
    demand = FleetDemandRow(
        observed_at=utc_now(),
        preemptible=False,
        gpu_types=(),
        cpu_millicores=1000,
        memory_mib=1024,
        gpu_count=0,
        count=0,
        pending_count=0,
        existing_containers=3,
        max_containers=5,
    )
    [row] = backlog_container_demand(
        [FleetBacklogRow(demand, queued_tasks=100, tasks_per_container=2)]
    )
    assert row.count == row.pending_count == 2
    assert (
        backlog_container_demand(
            [
                FleetBacklogRow(
                    replace(demand, existing_containers=5), queued_tasks=100, tasks_per_container=2
                )
            ]
        )
        == []
    )
