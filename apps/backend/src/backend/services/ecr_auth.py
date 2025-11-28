import base64
import json
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from loguru import logger
from models.registry import ECRCredentials

from backend.services.k8s import create_ns_name, create_release_name


class ECRAuthService:
    """Service for managing ECR authentication with per-tenant isolation."""

    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        base_role_arn: str,
        endpoint_url: str | None = None,
        ttl_seconds: int = 900,  # 15 minutes default
        pull_policy: str = "Always",  # Always pull for built images
    ):
        # Use config values if not provided
        self.account_id = account_id
        self.region = region
        self.endpoint_url = endpoint_url
        self.base_role_arn = base_role_arn
        self.ttl_seconds = ttl_seconds
        self.pull_policy = pull_policy
        self.is_localstack = (
            "localhost" in self.endpoint_url or "localstack" in self.endpoint_url
        )

        # Configure boto3 clients with appropriate settings
        client_kwargs = {
            "region_name": self.region,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
        }

        if self.endpoint_url:
            logger.info(f"Using custom endpoint: {self.endpoint_url}")
            client_kwargs["endpoint_url"] = self.endpoint_url
            client_kwargs["config"] = Config(
                region_name=self.region,
                signature_version="v4",
                retries={"max_attempts": 10, "mode": "standard"},
            )

        # Create clients with the same configuration
        self.sts_client = boto3.client("sts", **client_kwargs)
        self.ecr_client = boto3.client("ecr", **client_kwargs)

    def get_deployment_namespace(self, workspace_id: str, deployment_name: str) -> str:
        """Get the full namespace for a deployment (e.g., 'lc-<workspace_id>/deployment-<deployment_name>')"""
        namespace = create_ns_name(workspace_id)
        return f"{namespace}/deployment-{deployment_name}".lower()

    def get_repository_path(
        self, workspace_id: str, deployment_name: str, image_name: str
    ) -> str:
        """Get the full repository path without registry (e.g., 'lc-<workspace_id>/deployment-<deployment_name>/<image_name>')"""
        namespace = self.get_deployment_namespace(workspace_id, deployment_name)
        return f"{namespace}/{image_name}".lower()

    def get_pull_policy(self) -> str:
        """Get the pull policy for the ECR client"""
        return self.pull_policy

    def generate_session_policy(
        self, workspace_id: str, deployment_name: str, repo_name: str
    ) -> str:
        """Generate a session policy that restricts access to a specific tenant's repos"""
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:InitiateLayerUpload",
                        "ecr:UploadLayerPart",
                        "ecr:CompleteLayerUpload",
                        "ecr:PutImage",
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:DescribeImages",
                        "ecr:GetAuthorizationToken",
                    ],
                    "Resource": f"arn:aws:ecr:{self.region}:{self.account_id}:repository/{self.get_repository_path(workspace_id, deployment_name, repo_name)}/*",
                },
                {
                    "Effect": "Allow",
                    "Action": "ecr:GetAuthorizationToken",
                    "Resource": "*",
                },
            ],
        }

        return json.dumps(policy)

    async def ensure_repository(
        self, workspace_id: str, deployment_name: str, repo_name: str
    ) -> str:
        """Ensure ECR repository exists for tenant (lazy creation)"""
        # Strip tag if present (e.g., "myapp:latest" -> "myapp")
        if ":" in repo_name:
            repo_name = repo_name.split(":")[0]

        # Get the full repository path
        full_repo_name = self.get_repository_path(
            workspace_id, deployment_name, repo_name
        )

        try:
            # Check if repository exists
            self.ecr_client.describe_repositories(repositoryNames=[full_repo_name])
            logger.debug(f"Repository {full_repo_name} already exists")
        except ClientError as e:
            if e.response["Error"]["Code"] == "RepositoryNotFoundException":
                # Create repository
                logger.info(f"Creating ECR repository: {full_repo_name}")

                try:
                    response = self.ecr_client.create_repository(
                        repositoryName=full_repo_name,
                        imageScanningConfiguration={"scanOnPush": True},
                        imageTagMutability="MUTABLE",  # Allow dev tags to be overwritten
                        tags=[
                            {"Key": "tenant", "Value": workspace_id},
                            {"Key": "managed-by", "Value": "lazycloud"},
                        ],
                    )

                    # Try to add lifecycle policy
                    # dont use localstack for this
                    if not self.is_localstack:
                        try:
                            lifecycle_policy = {
                                "rules": [
                                    {
                                        "rulePriority": 1,
                                        "description": "Keep last 10 dev images",
                                        "selection": {
                                            "tagStatus": "tagged",
                                            "tagPrefixList": ["dev-", "build-"],
                                            "countType": "imageCountMoreThan",
                                            "countNumber": 10,
                                        },
                                        "action": {"type": "expire"},
                                    },
                                    {
                                        "rulePriority": 2,
                                        "description": "Keep release tags forever",
                                        "selection": {
                                            "tagStatus": "tagged",
                                            "tagPrefixList": ["v", "release-"],
                                            "countType": "sinceImagePushed",
                                            "countUnit": "days",
                                            "countNumber": 9999,
                                        },
                                        "action": {"type": "expire"},
                                    },
                                ]
                            }
                            self.ecr_client.put_lifecycle_policy(
                                repositoryName=full_repo_name,
                                lifecyclePolicyText=json.dumps(lifecycle_policy),
                            )
                        except Exception as policy_error:
                            logger.debug(
                                f"Could not set lifecycle policy: {policy_error}"
                            )

                    logger.info(
                        f"Created repository: {response['repository']['repositoryArn']}"
                    )
                except ClientError as create_error:
                    logger.error(f"Failed to create repository: {create_error}")
                    raise
            else:
                logger.error(f"Error checking repository: {e}")
                raise

        return full_repo_name

    async def delete_deployment_repositories(
        self, workspace_id: str, deployment_name: str
    ) -> list[str]:
        """Delete all ECR repositories for a deployment.

        Finds and deletes all repositories matching the pattern:
        lc-<workspace_id>/deployment-<deployment_name>/*

        Returns list of deleted repository names.
        """
        prefix = self.get_deployment_namespace(workspace_id, deployment_name)
        deleted_repos = []

        try:
            # List all repositories
            paginator = self.ecr_client.get_paginator("describe_repositories")

            for page in paginator.paginate():
                for repo in page.get("repositories", []):
                    repo_name = repo["repositoryName"]

                    # Check if repository belongs to this deployment
                    if repo_name.startswith(prefix + "/"):
                        try:
                            self.ecr_client.delete_repository(
                                repositoryName=repo_name,
                                force=True,  # Delete even if images exist
                            )
                            deleted_repos.append(repo_name)
                            logger.info(f"Deleted ECR repository: {repo_name}")
                        except ClientError as e:
                            # Log but don't fail - repository might already be deleted
                            logger.warning(
                                f"Failed to delete repository {repo_name}: {e}"
                            )

        except ClientError as e:
            logger.error(f"Failed to list repositories for cleanup: {e}")

        return deleted_repos

    def get_registry_url(self) -> str:
        """Get the registry URL based on environment (LocalStack or AWS)"""
        if self.is_localstack:
            # LocalStack ECR - use localhost for minikube compatibility
            # Both the host Docker and minikube can reach LocalStack via localhost
            return f"{self.account_id}.dkr.ecr.{self.region}.localhost:4566"
        else:
            # Real AWS ECR
            return f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"

    def get_repository_url(
        self, workspace_id: str, deployment_name: str, image_name: str
    ) -> str:
        """Get the full repository URL for an image."""
        registry_url = self.get_registry_url()
        repository_path = self.get_repository_path(
            workspace_id, deployment_name, image_name
        )
        url = f"{registry_url}/{repository_path}"
        return url

    async def get_upload_credentials(
        self,
        workspace_id: str,
        deployment_name: str,
        repo_name: str,
        session_name: str | None = None,
    ) -> ECRCredentials:
        """Get temporary ECR push credentials for a tenant"""

        # Generate session name if not provided
        if session_name is None:
            session_name = create_release_name(workspace_id, deployment_name)

        # Save the tag if present before ensuring repository
        tag = "latest"
        if ":" in repo_name:
            repo_name, tag = repo_name.split(":", 1)

        # Ensure repository exists (without tag)
        full_repo_name = await self.ensure_repository(
            workspace_id, deployment_name, repo_name
        )

        # For LocalStack/custom endpoints, return simplified credentials
        if self.is_localstack:
            # get_registry_url() already returns localhost for LocalStack
            registry_url = self.get_registry_url()
            repository_url = self.get_repository_url(
                workspace_id, deployment_name, repo_name
            )

            return ECRCredentials(
                registry_url=registry_url,
                username="AWS",
                password="localstack-token",  # LocalStack accepts any password
                repository=f"{repository_url}:{tag}",
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=self.ttl_seconds),
            )

        # Real AWS: Use STS AssumeRole with session policy
        session_policy = self.generate_session_policy(
            workspace_id, deployment_name, repo_name
        )

        try:
            assume_response = self.sts_client.assume_role(
                RoleArn=self.base_role_arn,
                RoleSessionName=session_name,
                Policy=session_policy,
                DurationSeconds=self.ttl_seconds,
            )

            credentials = assume_response["Credentials"]

            temp_ecr_client = boto3.client(
                "ecr",
                region_name=self.region,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )

            auth_response = temp_ecr_client.get_authorization_token()
            auth_data = auth_response["authorizationData"][0]

            registry_url = auth_data["proxyEndpoint"].replace("https://", "")
            auth_token = base64.b64decode(auth_data["authorizationToken"]).decode(
                "utf-8"
            )
            _, password = auth_token.split(":", 1)

            return ECRCredentials(
                registry_url=registry_url,
                username="AWS",
                password=password,
                repository=f"{registry_url}/{full_repo_name}:{tag}",
                expires_at=auth_data["expiresAt"],
            )

        except ClientError as e:
            logger.error(f"Failed to get upload credentials: {e}")
            raise

    async def check_images_exist(
        self, workspace_id: str, deployment_name: str, image_names: list[str]
    ) -> dict[str, bool]:
        """Check if images exist in ECR. Returns dict mapping image_name -> exists."""
        result = {}

        # For LocalStack, we can't reliably check, so return False for all
        if self.is_localstack:
            return {image_name: False for image_name in image_names}

        # Group images by repository to minimize API calls
        repo_to_images: dict[str, list[tuple[str, str]]] = {}
        for image_name in image_names:
            # Parse image name to extract repository and tag
            if ":" in image_name:
                repo_name, tag = image_name.rsplit(":", 1)
            else:
                repo_name = image_name
                tag = "latest"

            # Get the full repository path
            full_repo_name = self.get_repository_path(
                workspace_id, deployment_name, repo_name
            )

            if full_repo_name not in repo_to_images:
                repo_to_images[full_repo_name] = []
            repo_to_images[full_repo_name].append((image_name, tag))

        # Check each repository
        for full_repo_name, images in repo_to_images.items():
            # Initialize all images for this repo as False
            for image_name, _ in images:
                result[image_name] = False

            # Get all tags for this repository
            tags = [tag for _, tag in images]
            image_name_map = {tag: img_name for img_name, tag in images}

            try:
                # Batch check all tags for this repository
                response = self.ecr_client.describe_images(
                    repositoryName=full_repo_name,
                    imageIds=[{"imageTag": tag} for tag in tags],
                )

                # Mark found images as existing
                found_tags = {
                    img["imageTags"][0]
                    for img in response.get("imageDetails", [])
                    if img.get("imageTags")
                }

                for tag, image_name in image_name_map.items():
                    if tag in found_tags:
                        result[image_name] = True

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "RepositoryNotFoundException":
                    # Repository doesn't exist, all images don't exist
                    for _, image_name in images:
                        result[image_name] = False
                elif error_code == "ImageNotFoundException":
                    # Some images not found, check which ones individually
                    for image_name, tag in images:
                        try:
                            individual_response = self.ecr_client.describe_images(
                                repositoryName=full_repo_name,
                                imageIds=[{"imageTag": tag}],
                            )
                            result[image_name] = (
                                len(individual_response.get("imageDetails", [])) > 0
                            )
                        except Exception:
                            result[image_name] = False
                else:
                    # On other errors, assume doesn't exist
                    logger.warning(f"Error checking images in {full_repo_name}: {e}")
                    for _, image_name in images:
                        result[image_name] = False

            except Exception as e:
                logger.warning(f"Unexpected error checking images: {e}")
                # On error, assume doesn't exist
                for _, image_name in images:
                    result[image_name] = False

        return result
