from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from shared.contracts import ContractModel


class Autoscaler(ContractModel):
    """How many containers a workload wants for the work it can see.

    `min_containers` is a floor held with nothing queued — containers already up
    when a call arrives, so it does not pay for a start. For a function the floor
    also decides the keep-warm window: a container that retires itself after an
    idle window cannot be part of a count that is supposed to persist, so
    declaring a floor makes the window infinite and the autoscaler the only
    thing that removes one.
    """

    type: Literal["queue_depth"] = "queue_depth"
    min_containers: int = Field(default=0, ge=0)
    max_containers: int = Field(default=1, ge=0)
    tasks_per_container: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def minimum_cannot_exceed_maximum(self) -> Autoscaler:
        if self.min_containers > self.max_containers:
            msg = "min_containers cannot exceed max_containers"
            raise ValueError(msg)
        return self


__all__ = [
    "Autoscaler",
]
