"""Registry factory to create appropriate registry configuration."""

from enum import StrEnum

from lazycloud_api.registry.base import BaseRegistryConfig
from lazycloud_api.registry.custom import CustomRegistryConfig
from lazycloud_api.registry.docker_hub import DockerHubRegistryConfig
from lazycloud_api.registry.ecr import ECRRegistryConfig
from lazycloud_api.registry.minikube import MinikubeRegistryConfig


class RegistryType(StrEnum):
    """Supported registry types."""

    MINIKUBE = "minikube"
    DOCKER_HUB = "docker_hub"
    ECR = "ecr"
    CUSTOM = "custom"


def create_registry_config(
    registry_type: str | RegistryType, **kwargs
) -> BaseRegistryConfig:
    registry_type = RegistryType(registry_type)

    if registry_type == RegistryType.MINIKUBE:
        return MinikubeRegistryConfig(**kwargs)

    elif registry_type == RegistryType.DOCKER_HUB:
        return DockerHubRegistryConfig(**kwargs)

    elif registry_type == RegistryType.ECR:
        return ECRRegistryConfig(**kwargs)

    elif registry_type == RegistryType.CUSTOM:
        return CustomRegistryConfig(**kwargs)

    else:
        raise ValueError(f"Unsupported registry type: {registry_type}")
