"""Registry factory to create appropriate registry configuration."""

from enum import StrEnum

from backend.registry.base import BaseRegistryConfig
from backend.registry.custom import CustomRegistryConfig
from backend.registry.ecr import ECRRegistryConfig


class RegistryType(StrEnum):
    """Supported registry types."""

    ECR = "ecr"
    CUSTOM = "custom"


def create_registry_config(
    registry_type: str | RegistryType, **kwargs
) -> BaseRegistryConfig:
    registry_type = RegistryType(registry_type)

    if registry_type == RegistryType.ECR:
        return ECRRegistryConfig(**kwargs)

    elif registry_type == RegistryType.CUSTOM:
        return CustomRegistryConfig(**kwargs)
