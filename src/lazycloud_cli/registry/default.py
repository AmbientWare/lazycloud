"""
Default registry implementation for standard Docker registries.
"""

import subprocess
from pathlib import Path

from lazycloud_cli.registry.base import BaseRegistry


class DefaultRegistry(BaseRegistry):
    """Default registry implementation for standard Docker registries."""

    def setup(self) -> bool:
        """No special setup needed for default registry."""
        return True

    def build_image(
        self, image_name: str, context: Path, dockerfile: str = "Dockerfile"
    ) -> bool:
        """Build a Docker image."""
        dockerfile_path = context / dockerfile

        if not context.exists():
            return False

        if not dockerfile_path.exists():
            return False

        # Build command
        build_cmd = [
            "docker",
            "build",
            "-t",
            image_name,
            "-f",
            str(dockerfile_path),
            str(context),
        ]

        try:
            subprocess.run(
                build_cmd,
                check=True,
                capture_output=True,
                text=True,
            )
            return True

        except subprocess.CalledProcessError:
            return False

    def push_image(self, image_name: str) -> bool:
        """Push image to registry."""
        registry_image = self.get_image_url(image_name)

        # Tag the image for registry
        tag_cmd = ["docker", "tag", image_name, registry_image]
        try:
            subprocess.run(tag_cmd, check=True, capture_output=True, text=True)

        except subprocess.CalledProcessError:
            return False

        # Push to registry
        push_cmd = ["docker", "push", registry_image]
        try:
            subprocess.run(push_cmd, check=True, capture_output=True, text=True)
            return True

        except subprocess.CalledProcessError:
            return False

    def get_image_url(self, image_name: str) -> str:
        """Get the full registry URL for an image."""
        return f"{self.registry_url}/{image_name}"
