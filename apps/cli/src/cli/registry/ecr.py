import subprocess
from pathlib import Path
from typing import Optional

from responses.registry import UploadIntentResponse

from cli.api import api
from cli.registry.base import BaseRegistry, RegistryResponse


class ECRRegistry(BaseRegistry):
    """Registry implementation for AWS ECR (via LocalStack or real AWS)."""

    def __init__(self, deployment_name: str):
        super().__init__(deployment_name)
        self.credentials: Optional[UploadIntentResponse] = None
        self._logged_in: bool = False

    def setup(self) -> RegistryResponse:
        """Set up ECR authentication."""
        return RegistryResponse(success=True, error_message="")

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

    def docker_login(self) -> RegistryResponse:
        """Authenticate Docker with ECR."""
        if not self.credentials:
            return RegistryResponse(
                success=False, error_message="No credentials available"
            )

        # No need to login when running with local development
        if (
            "localhost" in self.credentials.registry_url
            or "localstack" in self.credentials.registry_url
        ):
            return RegistryResponse(success=True, error_message="")

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
            return RegistryResponse(
                success=login_result.returncode == 0, error_message=""
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            return RegistryResponse(
                success=False, error_message=f"Docker login failed: {error_msg}"
            )

    def build_image(
        self, image_name: str, context: Path, dockerfile: str = "Dockerfile"
    ) -> RegistryResponse:
        """Build a Docker image."""
        dockerfile_path = context / dockerfile

        if not context.exists():
            return RegistryResponse(
                success=False,
                error_message=f"Build context directory not found: {context}",
            )

        if not dockerfile_path.exists():
            return RegistryResponse(
                success=False, error_message=f"Dockerfile not found: {dockerfile_path}"
            )

        # Always get credentials for each image to ensure ECR repository creation
        try:
            self.credentials = self.get_upload_credentials(repo_name=image_name)
        except Exception as e:
            return RegistryResponse(success=False, error_message=str(e))

        # Only login once (registry URL is same for all images in deployment)
        if not self._logged_in:
            login_response = self.docker_login()
            if not login_response.success:
                return login_response
            self._logged_in = True

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
            return RegistryResponse(success=True, error_message="")
        except subprocess.CalledProcessError as e:
            # Extract useful error message from stderr
            error_output = e.stderr if e.stderr else e.stdout if e.stdout else str(e)
            return RegistryResponse(
                success=False, error_message=f"Docker build failed:\n{error_output}"
            )

    def push_image(self, image_name: str) -> RegistryResponse:
        """Push image to ECR."""
        if not self.credentials:
            return RegistryResponse(
                success=False,
                error_message="Must call build_image before push_image to establish credentials",
            )

        push_cmd = ["docker", "push", self.credentials.repository]
        try:
            _ = subprocess.run(push_cmd, check=True, capture_output=True, text=True)
            return RegistryResponse(success=True, error_message="")
        except subprocess.CalledProcessError as e:
            # Extract useful error message from stderr
            error_output = e.stderr if e.stderr else e.stdout if e.stdout else str(e)
            return RegistryResponse(
                success=False, error_message=f"Docker push failed:\n{error_output}"
            )

    def get_image_url(self, image_name: str) -> str:
        """Get the full registry URL for an image."""
        return self.credentials.repository

    def images_exist(self, image_names: list[str]) -> dict[str, bool]:
        """Check if images exist via API call."""
        if not image_names:
            return {}

        try:
            response = api.registry.check_images_exist(
                deployment_name=self.deployment_name,
                image_names=image_names,
            )
            return response.exists_map

        except Exception:
            # On error, assume doesn't exist (will trigger build)
            return {image_name: False for image_name in image_names}
