from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel


class RegistryResponse(BaseModel):
    """Response from the registry."""

    success: bool
    error_message: str


class BaseRegistry(ABC):
    """Base class for registry implementations."""

    def __init__(self, deployment_name: str):
        self.deployment_name = deployment_name

    @abstractmethod
    def setup(self) -> RegistryResponse:
        """Set up the registry environment (e.g., authentication, Docker config)"""
        pass

    @abstractmethod
    def build_image(
        self, image_name: str, context: Path, dockerfile: str = "Dockerfile"
    ) -> RegistryResponse:
        """Build a Docker image"""
        pass

    @abstractmethod
    def push_image(self, image_name: str) -> RegistryResponse:
        """Push an image to the registry"""
        pass

    @abstractmethod
    def get_image_url(self, image_name: str) -> str:
        """Get the full registry URL for an image."""
        pass
