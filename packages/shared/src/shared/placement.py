from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from shared.enums import StringEnum


class ProductRegion(StringEnum):
    UsEast = "us-east"
    UsWest = "us-west"
    EuCentral = "eu-central"
    EuNorth = "eu-north"
    ApSoutheast = "ap-southeast"


PlacementRateClass = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
]
AUTO_RATE_CLASS: PlacementRateClass = "auto"

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


def validate_region_pool(region: ProductRegion | None, pool: str | None) -> None:
    if region is not None and pool:
        raise ValueError("region and an explicit pool cannot be selected together")
