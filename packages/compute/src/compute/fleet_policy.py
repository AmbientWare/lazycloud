from __future__ import annotations

from pydantic import Field, model_validator
from shared.contracts import ContractModel


class FleetCapacityPolicy(ContractModel):
    minimum_purchase_margin_percent: int = Field(default=30, ge=0, lt=100)
    max_cpu_instances: int = Field(default=500, ge=0)
    max_gpu_instances: int = Field(default=100, ge=0)
    warm_cpu_preemptible_min: int = Field(default=1, ge=0)
    warm_cpu_non_preemptible_min: int = Field(default=0, ge=0)
    warm_decrease_after_seconds: int = Field(default=600, ge=60)

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
