"""Docker Hub registry configuration."""

from lazycloud_api.registry.base import BaseRegistryConfig


class DockerHubRegistryConfig(BaseRegistryConfig):
    """Registry configuration for Docker Hub."""

    username: str | None = None
    password: str | None = None

    @property
    def pull_policy(self) -> str:
        """Standard pull policy for Docker Hub."""
        return "IfNotPresent"

    @property
    def registry_prefix(self) -> str:
        """Docker Hub doesn't need a prefix."""
        return ""

    def format_image(self, image: str) -> str:
        """Format image for Docker Hub."""
        # Docker Hub images don't need a prefix
        return image

    def get_pull_secrets(self) -> list[dict] | None:
        """Get Docker Hub pull secrets if credentials provided."""
        if self.username and self.password:
            return [{"name": "docker-hub-secret"}]
        return None
