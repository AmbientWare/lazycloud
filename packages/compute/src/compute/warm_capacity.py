from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil

from database.repositories.compute import ComputeProviderInstanceRecord, PlatformCpuArrival
from shared.container_requests import schedulable_capacity
from shared.timestamps import to_utc

from compute.offers import ComputeOffer
from compute.providers import ResolvedProviderPolicy


@dataclass(frozen=True, slots=True)
class WarmCapacityTarget:
    machines: int
    lower_since: datetime | None


def warm_capacity_target(
    policy: ResolvedProviderPolicy,
    offer: ComputeOffer,
    arrivals: Sequence[PlatformCpuArrival],
    machines: Sequence[ComputeProviderInstanceRecord],
    *,
    current: int,
    lower_since: datetime | None,
    now: datetime,
) -> WarmCapacityTarget:
    launch_seconds = sorted(
        max((to_utc(machine.first_enrolled_at) - to_utc(machine.created_at)).total_seconds(), 1)
        for machine in machines
        if machine.first_enrolled_at is not None
    )
    horizon = (
        min(600, launch_seconds[ceil(len(launch_seconds) * 0.95) - 1]) if launch_seconds else 600
    )
    buckets = max(ceil(3600 / horizon), 1)
    cpu = [0] * buckets
    memory = [0] * buckets
    for arrival in arrivals:
        age = (to_utc(now) - to_utc(arrival.created_at)).total_seconds()
        if not 0 <= age < 3600:
            continue
        index = min(int(age // horizon), buckets - 1)
        cpu[index] += arrival.cpu_millicores
        memory[index] += arrival.reserved_memory_mib
    loads = sorted(
        max(
            ceil(cpu_value / schedulable_capacity(offer.cpu_millicores)),
            ceil(memory_value / schedulable_capacity(offer.memory_mb)),
        )
        for cpu_value, memory_value in zip(cpu, memory, strict=True)
    )
    target = min(policy.warm_cpu_max, max(policy.warm_cpu_min, loads[ceil(buckets * 0.95) - 1]))
    if target >= current:
        return WarmCapacityTarget(target, None)
    if lower_since is None:
        return WarmCapacityTarget(current, now)
    if now - lower_since < timedelta(seconds=policy.warm_decrease_after_seconds):
        return WarmCapacityTarget(current, lower_since)
    reduced = max(target, current - 1)
    return WarmCapacityTarget(reduced, now if reduced > target else None)
