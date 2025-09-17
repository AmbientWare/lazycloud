"""Base registry configuration."""

from abc import ABC, abstractmethod
from pydantic import BaseModel


class BaseRegistryConfig(BaseModel, ABC):
    """Abstract base class for registry configurations."""

    @property
    @abstractmethod
    def pull_policy(self) -> str:
        """Get the image pull policy for this registry."""
        pass

    @property
    @abstractmethod
    def registry_prefix(self) -> str:
        """Get the registry prefix for images."""
        pass

    @abstractmethod
    def format_image(self, image: str) -> str:
        """Format image name with registry prefix."""
        pass

    @abstractmethod
    def get_pull_secrets(self) -> list[dict] | None:
        """Get image pull secrets configuration if needed."""
        pass
