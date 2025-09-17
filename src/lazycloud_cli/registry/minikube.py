"""
Minikube registry implementation.
"""

import os
import subprocess
from pathlib import Path

from lazycloud_cli.registry.base import BaseRegistry


class MinikubeRegistry(BaseRegistry):
    """Registry implementation for Minikube."""

    def __init__(self, registry_url: str = "localhost:5000"):
        super().__init__(registry_url)
        self.using_minikube_docker = False

    def setup(self) -> bool:
        """Set up Minikube Docker environment."""
        try:
            # Check if minikube is running
            result = subprocess.run(
                ["minikube", "status"], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                return False

            # Get minikube docker env
            result = subprocess.run(
                ["minikube", "docker-env", "--shell=bash"],
                capture_output=True,
                text=True,
                check=True,
            )

            # Parse and set environment variables
            for line in result.stdout.strip().split("\n"):
                if line.startswith("export "):
                    key_value = line.replace("export ", "").strip()
                    if "=" in key_value:
                        key, value = key_value.split("=", 1)
                        value = value.strip('"').strip("'")
                        os.environ[key] = value

            self.using_minikube_docker = True
            return True

        except Exception:
            return False

    def build_image(
        self, image_name: str, context: Path, dockerfile: str = "Dockerfile"
    ) -> bool:
        """Build a Docker image using Minikube's Docker daemon."""
        dockerfile_path = context / dockerfile

        if not context.exists():
            return False

        if not dockerfile_path.exists():
            return False

        # Build command - use classic builder to avoid buildx issues
        build_cmd = [
            "docker",
            "build",
            "-t",
            image_name,
            "-f",
            str(dockerfile_path),
            str(context),
        ]

        # Set DOCKER_BUILDKIT=0 to use classic builder
        env = os.environ.copy()
        env["DOCKER_BUILDKIT"] = "0"

        try:
            subprocess.run(
                build_cmd,
                check=True,
                capture_output=True,
                text=True,
                env=env,  # Use modified environment
            )
            return True

        except subprocess.CalledProcessError:
            return False

    def push_image(self, image_name: str) -> bool:
        """Push image to Minikube registry."""
        if self.using_minikube_docker:
            # Images built with Minikube's Docker are already available in the cluster
            return True

        # If not using Minikube's Docker, we need to push to the registry
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
        if self.using_minikube_docker:
            # When using Minikube's Docker, we can use the image name directly
            return image_name
        return f"{self.registry_url}/{image_name}"
