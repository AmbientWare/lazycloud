"""Translate function invocations into additional container allocations."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from database.repositories.fleet_demand import FleetBacklogRow, FleetDemandRow
from shared.autoscaling import (
    BacklogAutoscalerConfig,
    BacklogAutoscalerSample,
    decide_backlog_scale,
    function_container_ceiling,
)


def backlog_container_demand(rows: Sequence[FleetBacklogRow]) -> list[FleetDemandRow]:
    result: list[FleetDemandRow] = []
    for row in rows:
        demand = row.demand
        decision = decide_backlog_scale(
            BacklogAutoscalerSample(
                queue_length=row.queued_tasks,
                current_containers=demand.existing_containers,
            ),
            BacklogAutoscalerConfig(
                tasks_per_container=row.tasks_per_container,
                max_containers=function_container_ceiling(demand.max_containers),
            ),
        )
        missing = max(decision.desired_containers - demand.existing_containers, 0)
        if missing:
            result.append(replace(demand, count=missing, pending_count=missing))
    return result


@dataclass(slots=True)
class _Container:
    created_at: datetime | None
    expires_at: datetime
    finishes: list[datetime] = field(default_factory=list)


def _allocation(shape: FleetDemandRow, container: _Container) -> FleetDemandRow | None:
    if container.created_at is None:
        return None
    return replace(
        shape,
        observed_at=container.created_at,
        count=1,
        pending_count=0,
        duration_seconds=(container.expires_at - container.created_at).total_seconds(),
    )


def scheduled_container_demand(
    rows: Sequence[FleetDemandRow], *, now: datetime, until: datetime
) -> list[FleetDemandRow]:
    workloads: dict[str, list[FleetDemandRow]] = defaultdict(list)
    for row in rows:
        workloads[row.workload_id].append(row)
    result: list[FleetDemandRow] = []
    for occurrences in workloads.values():
        ordered = sorted(occurrences, key=lambda row: row.observed_at)
        shape = ordered[0]
        retained_until = until + timedelta(seconds=1)
        containers: list[_Container] = []
        # A finite idle window has no durable remaining time. Only an explicit
        # warm floor establishes that today's container will still be here.
        if shape.keep_warm_seconds == -1:
            containers = [
                _Container(None, retained_until) for _ in range(shape.existing_containers)
            ]

        for occurrence in ordered:
            at = occurrence.observed_at
            for container in tuple(containers):
                container.finishes = [end for end in container.finishes if end > at]
                if container.expires_at <= at and not container.finishes:
                    if allocation := _allocation(shape, container):
                        result.append(allocation)
                    containers.remove(container)
            for _ in range(occurrence.count):
                available = next(
                    (c for c in containers if len(c.finishes) < shape.concurrency), None
                )
                starts_at = at
                if available is None:
                    if len(containers) >= function_container_ceiling(shape.max_containers):
                        available = min(containers, key=lambda c: min(c.finishes))
                        starts_at = min(available.finishes)
                        available.finishes.remove(starts_at)
                    else:
                        available = _Container(at, until)
                        containers.append(available)
                duration = occurrence.duration_seconds or max((until - now).total_seconds(), 1)
                finished_at = starts_at + timedelta(seconds=duration)
                available.finishes.append(finished_at)
                available.expires_at = (
                    retained_until
                    if shape.keep_warm_seconds == -1
                    else max(available.finishes) + timedelta(seconds=shape.keep_warm_seconds)
                )
        for container in containers:
            if allocation := _allocation(shape, container):
                result.append(allocation)
    return result
