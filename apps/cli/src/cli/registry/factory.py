from enum import StrEnum

from cli.registry.base import BaseRegistry
from cli.registry.ecr import ECRRegistry


class RegistryType(StrEnum):
    ECR = "ecr"


def create_registry(registry_type: RegistryType, deployment_name: str) -> BaseRegistry:
    """Create the appropriate registry implementation based on the environment"""
    if registry_type == RegistryType.ECR:
        return ECRRegistry(deployment_name)
