import aws_cdk as cdk
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class SharedSecretsConstruct(Construct):
    """Manages shared secrets in AWS Secrets Manager for all environments"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create centralized shared-secrets for the organization
        # This holds all secrets that need to be synced to Kubernetes across environments
        # IMPORTANT: Populate this secret before deploying environment stacks (prod/dev)
        self.shared_secrets = secretsmanager.Secret(
            self,
            "SharedSecrets",
            secret_name=f"{config.org_name}/shared-secrets",
            description=f"Shared secrets for {config.org_name} - synced to Kubernetes via External Secrets Operator",
            # Using secret_string_value allows you to populate it later via AWS CLI
            # The secret will be created with placeholder, then you populate it before deploying env stacks
            secret_string_value=cdk.SecretValue.unsafe_plain_text(
                '{"CLOUDFLARE_API_TOKEN":"PLACEHOLDER"}'
            ),
        )

        # Add tags
        cdk.Tags.of(self.shared_secrets).add("Environment", "shared")
        cdk.Tags.of(self.shared_secrets).add("Purpose", "SharedSecrets")
        cdk.Tags.of(self.shared_secrets).add("Organization", config.org_name)
        cdk.Tags.of(self.shared_secrets).add("ManagedBy", "ExternalSecretsOperator")

    @property
    def secret_arn(self) -> str:
        """Get the secret ARN"""
        return self.shared_secrets.secret_arn

    @property
    def secret_name(self) -> str:
        """Get the secret name"""
        return self.shared_secrets.secret_name
