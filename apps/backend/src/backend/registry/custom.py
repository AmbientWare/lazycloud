"""Custom registry configuration."""

from backend.registry.base import BaseRegistryConfig


class CustomRegistryConfig(BaseRegistryConfig):
    """Registry configuration for custom/private registries."""

    registry_url: str  # e.g., myregistry.company.com:5000
    username: str | None = None
    password: str | None = None
    secret_name: str = "custom-registry-secret"

    @property
    def pull_policy(self) -> str:
        """Get configured pull policy."""
        return "IfNotPresent"

    @property
    def registry_prefix(self) -> str:
        """Get custom registry URL."""
        return self.registry_url

    def format_image(self, image: str) -> str:
        """Format image for custom registry."""
        # Remove any existing registry prefix
        if "/" in image and not image.startswith("library/"):
            parts = image.split("/", 1)
            if "." in parts[0] or ":" in parts[0]:
                image = parts[1] if len(parts) > 1 else image

        return f"{self.registry_prefix}/{image}"

    def get_pull_secrets(self) -> list[dict] | None:
        """Get pull secrets if credentials provided."""
        if self.username and self.password:
            return [{"name": self.secret_name}]
        return None
