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
    ready: Mapping[str, Capacity] = field(default_factory=lambda: dict[str, Capacity]())
    required_ready: Mapping[str, Capacity] = field(default_factory=lambda: dict[str, Capacity]())
    reserved_replacements: frozenset[str] = frozenset()


def plan_maintenance(
    candidates: Sequence[MaintenanceCandidate], budget: MaintenanceBudget
) -> tuple[MaintenanceCandidate, ...]:
    """Select exclusive replacements while preserving ready capacity."""
    ready = dict(budget.ready)
    replacements = set(budget.reserved_replacements)
    selected: list[MaintenanceCandidate] = []
    sources: set[str] = set()
    for candidate in candidates:
        if (
            candidate.machine_id in sources
            or candidate.machine_id in replacements
            or candidate.replacement_machine_id in replacements
            or candidate.replacement_machine_id in sources
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
    return tuple(selected)
