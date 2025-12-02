import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class ECRRepository(Construct):
    """
    ECR Repository construct with lifecycle policies and security settings
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        repository_name: str,
        enable_image_scan: bool = True,
        enable_immutable_tags: bool = False,
        max_image_count: int = 25,
        lifecycle_rules: list[ecr.LifecycleRule] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create the ECR repository
        self.repository = ecr.Repository(
            self,
            f"{repository_name}Repository",
            repository_name=repository_name,
            image_scan_on_push=enable_image_scan,
            image_tag_mutability=(
                ecr.TagMutability.IMMUTABLE
                if enable_immutable_tags
                else ecr.TagMutability.MUTABLE
            ),
            lifecycle_rules=lifecycle_rules
            or self._get_default_lifecycle_rules(max_image_count),
            removal_policy=cdk.RemovalPolicy.RETAIN,  # Protect against accidental deletion
        )

        # Add tags
        cdk.Tags.of(self.repository).add("Environment", config.environment)
        cdk.Tags.of(self.repository).add("Organization", config.org_name)
        cdk.Tags.of(self.repository).add("ManagedBy", "AWS-CDK")
        cdk.Tags.of(self.repository).add("Service", repository_name)

    def _get_default_lifecycle_rules(
        self, max_image_count: int
    ) -> list[ecr.LifecycleRule]:
        """
        Get default lifecycle rules for the repository
        """
        return [
            # Keep only the most recent images (ANY status, highest priority = highest number)
            ecr.LifecycleRule(
                description=f"Keep only {max_image_count} most recent images",
                max_image_count=max_image_count,
                rule_priority=2,  # Higher number = higher priority in ECR
                tag_status=ecr.TagStatus.ANY,
            ),
            # Clean up untagged images quickly (lower priority = lower number)
            ecr.LifecycleRule(
                description="Delete untagged images after 1 day",
                max_image_age=cdk.Duration.days(1),
                rule_priority=1,  # Lower number = lower priority in ECR
                tag_status=ecr.TagStatus.UNTAGGED,
            ),
        ]

    def grant_pull(self, identity) -> iam.Grant:
        """Grant pull permissions to an identity"""
        return self.repository.grant_pull(identity)

    def grant_push(self, identity) -> iam.Grant:
        """Grant push permissions to an identity"""
        return self.repository.grant_push(identity)

    def grant_pull_push(self, identity) -> iam.Grant:
        """Grant both pull and push permissions to an identity"""
        return self.repository.grant_pull_push(identity)

    @property
    def repository_uri(self) -> str:
        """Get the repository URI"""
        return self.repository.repository_uri

    @property
    def repository_arn(self) -> str:
        """Get the repository ARN"""
        return self.repository.repository_arn

    @property
    def repository_name(self) -> str:
        """Get the repository name"""
        return self.repository.repository_name

    def create_outputs(self, stack: cdk.Stack, service_name: str) -> None:
        """Create CloudFormation outputs for the ECR repository"""
        cdk.CfnOutput(
            stack,
            f"{service_name}ECRRepositoryURI",
            value=self.repository_uri,
            description=f"ECR Repository URI for {service_name}",
            export_name=f"{stack.stack_name}-{service_name}-ECR-URI",
        )

        cdk.CfnOutput(
            stack,
            f"{service_name}ECRRepositoryARN",
            value=self.repository_arn,
            description=f"ECR Repository ARN for {service_name}",
            export_name=f"{stack.stack_name}-{service_name}-ECR-ARN",
        )

        cdk.CfnOutput(
            stack,
            f"{service_name}ECRRepositoryName",
            value=self.repository_name,
            description=f"ECR Repository Name for {service_name}",
            export_name=f"{stack.stack_name}-{service_name}-ECR-Name",
        )


class MultiServiceECRRepositories(Construct):
    """
    Create multiple ECR repositories for different services
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        services: list[str],
        **repository_kwargs,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.repositories = {}

        # Create ECR repository for each service
        for service_name in services:
            repo = ECRRepository(
                self,
                f"{service_name}ECR",
                config=config,
                repository_name=service_name,
                **repository_kwargs,
            )
            self.repositories[service_name] = repo

    def get_repository(self, service_name: str) -> ECRRepository:
        """Get a specific repository by service name"""
        if service_name not in self.repositories:
            raise ValueError(f"Repository for service '{service_name}' not found")

        return self.repositories[service_name]

    def create_outputs(self, stack: cdk.Stack) -> None:
        """Create outputs for all repositories"""
        for service_name, repo in self.repositories.items():
            repo.create_outputs(stack, service_name)
