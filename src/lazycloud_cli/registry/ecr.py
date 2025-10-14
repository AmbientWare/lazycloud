import subprocess
from pathlib import Path
from typing import Optional

from lazycloud_cli.api import api
from lazycloud_cli.registry.base import BaseRegistry
from shared.responses.registry import UploadIntentResponse


class ECRRegistry(BaseRegistry):
    """Registry implementation for AWS ECR (via LocalStack or real AWS)."""

    def __init__(self, deployment_name: str):
        super().__init__(deployment_name)
        self.credentials: Optional[UploadIntentResponse] = None

    def setup(self) -> bool:
        """Set up ECR authentication."""
        return True

    def get_upload_credentials(self, repo_name: str) -> UploadIntentResponse:
        """Get temporary ECR push credentials from the API."""
        try:
            response = api.registry.get_upload_intent(
                deployment_name=self.deployment_name,
                repo_name=repo_name,
            )
            self.credentials = response
            self.registry_url = response.registry_url
            return response

        except Exception as e:
            raise RuntimeError(f"Failed to get ECR credentials: {e}")

    def docker_login(self) -> bool:
        """Authenticate Docker with ECR."""
        if not self.credentials:
            return False

        # No need to login when running with local development
        if (
            "localhost" in self.credentials.registry_url
            or "localstack" in self.credentials.registry_url
        ):
            print("Skipping Docker login for LocalStack")
            return True

        try:
            # Use the password from credentials to login with docker
            login_result = subprocess.run(
                [
                    "docker",
                    "login",
                    "--username",
                    self.credentials.username,
                    "--password-stdin",
                    self.credentials.registry_url,
                ],
                input=self.credentials.password,  # No need to encode when text=True
                capture_output=True,
                text=True,
                check=True,
            )
            return login_result.returncode == 0
        except subprocess.CalledProcessError:
            return False

    def build_image(
        self, image_name: str, context: Path, dockerfile: str = "Dockerfile"
    ) -> bool:
        """Build a Docker image."""
        dockerfile_path = context / dockerfile

        if not context.exists():
            return False

        if not dockerfile_path.exists():
            return False

        # Get credentials first to know the repository URL
        if not self.credentials:
            self.credentials = self.get_upload_credentials(repo_name=image_name)
            if not self.docker_login():
                return False

        # Build with the full ECR repository URL
        full_image_url = self.get_image_url(image_name)

        build_cmd = [
            "docker",
            "build",
            "-t",
            full_image_url,
            "-f",
            str(dockerfile_path),
            str(context),
        ]

        try:
            _ = subprocess.run(build_cmd, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def push_image(self, image_name: str) -> bool:
        """Push image to ECR."""
        # Ensure we have credentials
        if not self.credentials:
            self.credentials = self.get_upload_credentials(repo_name=image_name)
            if not self.docker_login():
                return False

        # If the image was built with a different name, tag it
        if image_name != self.credentials.repository:
            tag_cmd = ["docker", "tag", image_name, self.credentials.repository]
            try:
                subprocess.run(tag_cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError:
                return False

        # Push to ECR
        push_cmd = ["docker", "push", self.credentials.repository]
        try:
            _ = subprocess.run(push_cmd, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def get_image_url(self, image_name: str) -> str:
        """Get the full registry URL for an image."""
        return self.credentials.repository
