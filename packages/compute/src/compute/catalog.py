from __future__ import annotations

from dataclasses import dataclass

from shared.aws_connections import AwsAccountComputeConfiguration
from shared.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class ComputeCatalogInstance:
    instance_type: str
    kind: str
    cpu_millicores: int
    memory_mb: int
    gpu: str | None = None
    gpu_count: int = 0


@dataclass(frozen=True, slots=True)
class ComputeCatalogRegion:
    region: str
    instances: tuple[ComputeCatalogInstance, ...]


def validate_aws_compute_configuration(
    configuration: AwsAccountComputeConfiguration,
    *,
    catalog: tuple[ComputeCatalogRegion, ...],
) -> None:
    """Reject a configuration naming capacity this deployment cannot launch.

    The configuration's own validator settles what is internally consistent; this
    settles what the deployment's provider inventory actually offers, which the
    contract has no way to know.
    """
    instances_by_region = {
        region.region: {instance.instance_type for instance in region.instances}
        for region in catalog
    }
    unavailable_regions = set(configuration.allowed_regions) - instances_by_region.keys()
    if unavailable_regions:
        raise InvalidInputError(
            "AWS regions are not available for configured capacity: "
            + ", ".join(sorted(unavailable_regions))
        )
    available_types = {
        instance_type
        for region in configuration.allowed_regions
        for instance_type in instances_by_region[region]
    }
    unavailable_types = set(configuration.allowed_instance_types) - available_types
    if unavailable_types:
        raise InvalidInputError(
            "AWS instance types are not available in allowed regions: "
            + ", ".join(sorted(unavailable_types))
        )
    if configuration.default_instance_type not in instances_by_region[configuration.default_region]:
        raise InvalidInputError(
            "AWS default instance type is not available in the default region: "
            f"{configuration.default_instance_type}"
        )


__all__ = [
    "ComputeCatalogInstance",
    "ComputeCatalogRegion",
    "validate_aws_compute_configuration",
]
