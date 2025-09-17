"""Minikube registry configuration."""

from lazycloud_api.registry.base import BaseRegistryConfig


class MinikubeRegistryConfig(BaseRegistryConfig):
    """Registry configuration for Minikube local development."""

    registry_url: str = "localhost:5000"

    @property
    def pull_policy(self) -> str:
        """Never pull - use local Docker daemon images."""
        return "Never"

    @property
    def registry_prefix(self) -> str:
        """Get the registry prefix for images."""
        return self.registry_url

    def format_image(self, image: str) -> str:
        """Format image for Minikube registry."""
        # For Minikube, we don't need to push to registry if using Never pull policy
        # Just use the image name as-is
        return image

    def get_pull_secrets(self) -> list[dict] | None:
        """No pull secrets needed for Minikube."""
        return None
