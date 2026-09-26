from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from compute.fleet_resources import Capacity


@dataclass(frozen=True, slots=True)
class MaintenanceCandidate:
    machine_id: str
    market: str
    unavailable: Capacity = field(default_factory=Capacity)
    surge_machines: int = 0
    running_cpu_millicores: int = 0
    hourly_cost_micros: int | None = None
    replacement_machine_id: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceBudget:
    available_operations: int
    available_machines: int
    available_running_cpu_millicores: int
    available_hourly_cost_micros: int
    ready: Mapping[str, Capacity] = field(default_factory=lambda: dict[str, Capacity]())
    required_ready: Mapping[str, Capacity] = field(default_factory=lambda: dict[str, Capacity]())
    reserved_replacements: frozenset[str] = frozenset()
    unknown_committed_cost: bool = False


def plan_maintenance(
    candidates: Sequence[MaintenanceCandidate], budget: MaintenanceBudget
) -> tuple[MaintenanceCandidate, ...]:
    """Reserve each selected machine's capacity and spend once within this batch."""
    operations = budget.available_operations
    machines = max(budget.available_machines, 0)
    cpu = max(budget.available_running_cpu_millicores, 0)
    cost = max(budget.available_hourly_cost_micros, 0)
    ready = dict(budget.ready)
    replacements = set(budget.reserved_replacements)
    selected: list[MaintenanceCandidate] = []
    sources: set[str] = set()
    for candidate in candidates:
        if operations <= 0:
            break
        if (
            candidate.machine_id in sources
            or candidate.machine_id in replacements
            or candidate.replacement_machine_id in replacements
            or candidate.replacement_machine_id in sources
            or candidate.hourly_cost_micros is None
            or (budget.unknown_committed_cost and candidate.hourly_cost_micros > 0)
            or candidate.surge_machines > machines
            or candidate.running_cpu_millicores > cpu
            or candidate.hourly_cost_micros > cost
        ):
            continue
        current = ready.get(candidate.market, Capacity())
        required = current.lower(budget.required_ready.get(candidate.market, Capacity()))
        remaining = current - candidate.unavailable
        if not remaining.covers(required):
            continue
        selected.append(candidate)
        sources.add(candidate.machine_id)
        if candidate.replacement_machine_id is not None:
            replacements.add(candidate.replacement_machine_id)
        ready[candidate.market] = remaining
        operations -= 1
        machines -= candidate.surge_machines
        cpu -= candidate.running_cpu_millicores
        cost -= candidate.hourly_cost_micros
    return tuple(selected)
