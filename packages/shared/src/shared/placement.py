from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_serializer, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum


class PlacementKind(StringEnum):
    Platform = "platform"
    Connection = "connection"
    Machine = "machine"


PLACEMENT_KEY_MAX_LENGTH = 120


class Placement(ContractModel):
    """Where a workload runs: the platform fleet, one connected account, or one joined machine.

    An identity, not a label. Two placements are the same capacity only when they
    compare equal, which is what lets the scheduler match a worker to a request
    with one comparison and no owner check. `key` is the durable and wire form.
    """

    model_config = ContractModel.model_config | {"frozen": True}

    kind: PlacementKind
    id: str = Field(default="", max_length=PLACEMENT_KEY_MAX_LENGTH - 16)

    @model_validator(mode="before")
    @classmethod
    def from_key(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        kind, separator, identifier = value.strip().partition(":")
        if not separator and kind != PlacementKind.Platform.value:
            raise ValueError(f"invalid placement key {value!r}")
        return {"kind": kind, "id": identifier}

    @model_serializer(mode="plain")
    def to_key(self) -> str:
        return self.key

    @model_validator(mode="after")
    def validate_id(self) -> Placement:
        if self.kind is PlacementKind.Platform:
            if self.id:
                raise ValueError("platform placement carries no id")
        elif not self.id or ":" in self.id:
            raise ValueError(f"{self.kind.value} placement requires an id")
        return self

    @property
    def key(self) -> str:
        if self.kind is PlacementKind.Platform:
            return PlacementKind.Platform.value
        return f"{self.kind.value}:{self.id}"

    def __str__(self) -> str:
        return self.key

    @classmethod
    def parse(cls, key: str) -> Placement:
        return cls.model_validate(key)

    @classmethod
    def platform(cls) -> Placement:
        return cls(kind=PlacementKind.Platform)

    @classmethod
    def connection(cls, connection_id: str) -> Placement:
        return cls(kind=PlacementKind.Connection, id=connection_id)

    @classmethod
    def machine(cls, machine_id: str) -> Placement:
        return cls(kind=PlacementKind.Machine, id=machine_id)


class ProductRegion(StringEnum):
    UsEast = "us-east"
    UsWest = "us-west"
    EuCentral = "eu-central"
    EuNorth = "eu-north"
    ApSoutheast = "ap-southeast"


AvailabilityZone = Annotated[str, StringConstraints(max_length=128, pattern=r"^[A-Za-z0-9._:-]*$")]


PlacementRateClass = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
]
AUTO_RATE_CLASS: PlacementRateClass = "auto"
PINNED_RATE_CLASS: PlacementRateClass = "pinned"
NON_PREEMPTIBLE_RATE_CLASS: PlacementRateClass = "non_preemptible"
PINNED_NON_PREEMPTIBLE_RATE_CLASS: PlacementRateClass = "pinned_non_preemptible"


def placement_rate_class(*, pinned: bool, preemptible: bool) -> PlacementRateClass:
    if preemptible:
        return PINNED_RATE_CLASS if pinned else AUTO_RATE_CLASS
    return PINNED_NON_PREEMPTIBLE_RATE_CLASS if pinned else NON_PREEMPTIBLE_RATE_CLASS


_PROVIDER_REGIONS: dict[str, ProductRegion] = {
    "us-east-1": ProductRegion.UsEast,
    "us-east-2": ProductRegion.UsEast,
    "us-west-1": ProductRegion.UsWest,
    "us-west-2": ProductRegion.UsWest,
    "eu-central-1": ProductRegion.EuCentral,
    "eu-central-2": ProductRegion.EuCentral,
    "eu-north-1": ProductRegion.EuNorth,
    "ap-southeast-1": ProductRegion.ApSoutheast,
    "ap-southeast-3": ProductRegion.ApSoutheast,
    "ap-southeast-5": ProductRegion.ApSoutheast,
    "ash": ProductRegion.UsEast,
    "hil": ProductRegion.UsWest,
    "fsn1": ProductRegion.EuCentral,
    "nbg1": ProductRegion.EuCentral,
    "hel1": ProductRegion.EuNorth,
    "sin": ProductRegion.ApSoutheast,
}


def product_region(provider_region: str) -> ProductRegion | None:
    return _PROVIDER_REGIONS.get(provider_region)


def validate_placement_machine(
    region: ProductRegion | None, availability_zone: str, machine: str | None
) -> None:
    if (region is not None or availability_zone) and machine:
        raise ValueError(
            "region or availability zone and a named machine cannot be selected together"
        )
