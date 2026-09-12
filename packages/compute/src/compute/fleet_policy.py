from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field, model_validator
from shared.contracts import ContractModel


class FleetCapacityPolicy(ContractModel):
    minimum_purchase_margin_percent: int = Field(default=30, ge=0, lt=100)
    max_cpu_instances: int = Field(default=4, ge=0)
    max_gpu_instances: int = Field(default=100, ge=0)
    warm_cpu_preemptible_min: int = Field(default=2, ge=0)
    warm_cpu_non_preemptible_min: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_warm_capacity(self) -> FleetCapacityPolicy:
        if (
            self.warm_cpu_preemptible_min + self.warm_cpu_non_preemptible_min
            > self.max_cpu_instances
        ):
            raise ValueError("CPU warm minimums cannot exceed the fleet CPU node limit")
        return self

    def machine_limit(self, *, gpu: bool) -> int:
        return self.max_gpu_instances if gpu else self.max_cpu_instances

    def warm_cpu_min(self, *, preemptible: bool) -> int:
        return self.warm_cpu_preemptible_min if preemptible else self.warm_cpu_non_preemptible_min


@dataclass(frozen=True, slots=True)
class WarmCapacityUnit:
    unit_id: str
    desired: int
    committed: int
    ready: int
    floor: int
    eligible: bool
    handoff_from: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WarmCapacityPlan:
    target_machines: int
    floors: dict[str, int]
    handoff_from: tuple[str, ...]


def plan_warm_capacity(
    units: tuple[WarmCapacityUnit, ...],
    *,
    target_unit_id: str,
    minimum: int,
    fleet_baseline: int,
    fleet_limit: int,
    fleet_committed: int,
    maintenance_busy: bool,
) -> WarmCapacityPlan:
    target = next((unit for unit in units if unit.unit_id == target_unit_id), None)
    other_eligible = sum(
        min(unit.ready, unit.desired)
        for unit in units
        if unit.unit_id != target_unit_id and unit.eligible
    )
    required = max(minimum - other_eligible, 0)
    current_desired = target.desired if target is not None else 0
    current_committed = target.committed if target is not None else 0
    ceiling = min(fleet_limit, fleet_baseline + int(not maintenance_busy))
    desired = max(
        current_desired,
        min(required, max(ceiling - fleet_committed + current_committed, 0)),
    )
    floors = {unit.unit_id: 0 for unit in units}
    floors[target_unit_id] = min(desired, minimum)
    remaining = max(minimum - min(target.ready if target else 0, desired), 0)
    for unit in sorted(
        (unit for unit in units if unit.unit_id != target_unit_id),
        key=lambda unit: (not unit.eligible, -unit.floor, unit.unit_id),
    ):
        retained = min(unit.ready, unit.desired, remaining)
        floors[unit.unit_id] = retained
        remaining -= retained
    sources = set(target.handoff_from if target else ())
    sources.update(
        unit.unit_id
        for unit in units
        if unit.unit_id != target_unit_id and not unit.eligible and unit.floor
    )
    handoff_from = tuple(
        sorted(
            unit.unit_id
            for unit in units
            if unit.unit_id in sources
            and unit.committed
            and (not unit.eligible or unit.committed > floors[unit.unit_id])
        )
    )
    return WarmCapacityPlan(desired, floors, handoff_from)
