"""AWS ECR registry configuration."""

from lazycloud_api.registry.base import BaseRegistryConfig


class ECRRegistryConfig(BaseRegistryConfig):
    """Registry configuration for AWS ECR."""

    registry_url: str  # e.g., 123456789.dkr.ecr.us-east-1.amazonaws.com
    region: str = "us-east-1"

    @property
    def pull_policy(self) -> str:
        """Standard pull policy for ECR."""
        return "IfNotPresent"

    @property
    def registry_prefix(self) -> str:
        """Get ECR registry URL."""
        return self.registry_url

    def format_image(self, image: str) -> str:
        """Format image for ECR."""
        # Remove any existing registry prefix
        if "/" in image and not image.startswith("library/"):
            parts = image.split("/", 1)
            if "." in parts[0] or ":" in parts[0]:
                image = parts[1] if len(parts) > 1 else image

        return f"{self.registry_prefix}/{image}"

    def get_pull_secrets(self) -> list[dict] | None:
        """Get ECR pull secrets."""
        return [{"name": "ecr-secret"}]
