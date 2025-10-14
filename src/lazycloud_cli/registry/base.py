from abc import ABC, abstractmethod
from pathlib import Path


class BaseRegistry(ABC):
    """Base class for registry implementations."""

    def __init__(self, deployment_name: str):
        self.deployment_name = deployment_name

    @abstractmethod
    def setup(self) -> bool:
        """Set up the registry environment (e.g., authentication, Docker config)."""
        pass

    @abstractmethod
    def build_image(
        self, image_name: str, context: Path, dockerfile: str = "Dockerfile"
    ) -> bool:
        """Build a Docker image."""
        pass

    @abstractmethod
    def push_image(self, image_name: str) -> bool:
        """Push an image to the registry."""
        pass

    @abstractmethod
    def get_image_url(self, image_name: str) -> str:
        """Get the full registry URL for an image."""
        pass

    def cleanup(self) -> None:
        """Clean up any resources (optional)."""
        pass
