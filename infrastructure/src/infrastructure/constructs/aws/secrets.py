import aws_cdk as cdk
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class SharedSecretsConstruct(Construct):
    """Manages shared secrets in AWS Secrets Manager.

    Creates empty secrets that should be populated manually via AWS CLI:
    - {org_name}/shared-secrets: Cloudflare API token, etc.
    - {org_name}/prod-secrets: Production app secrets (DATABASE_URL, etc.)
    - {org_name}/staging-secrets: Staging app secrets

    Example to populate:
        aws secretsmanager put-secret-value --secret-id lazycloud/prod-secrets \\
            --secret-string '{"DATABASE_URL":"...", "REDIS_URL":"..."}'
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Shared secrets (Cloudflare, etc.)
        self.shared_secrets = self._create_secret(
            "SharedSecrets",
            f"{config.org_name}/shared-secrets",
            "Shared secrets (Cloudflare, etc.) - synced to kube-system",
            "shared",
        )

        # Prod app secrets
        self.prod_secrets = self._create_secret(
            "ProdSecrets",
            f"{config.org_name}/prod-secrets",
            "Production app secrets - synced to lazycloud-prod namespace",
            "prod",
        )

        # Staging app secrets
        self.staging_secrets = self._create_secret(
            "StagingSecrets",
            f"{config.org_name}/staging-secrets",
            "Staging app secrets - synced to lazycloud-staging namespace",
            "staging",
        )

    def _create_secret(
        self, construct_id: str, secret_name: str, description: str, environment: str
    ) -> secretsmanager.Secret:
        """Create a secret with standard tags."""
        secret = secretsmanager.Secret(
            self,
            construct_id,
            secret_name=secret_name,
            description=f"{description} for {self.config.org_name}",
        )
        cdk.Tags.of(secret).add("Environment", environment)
        cdk.Tags.of(secret).add("Organization", self.config.org_name)
        cdk.Tags.of(secret).add("ManagedBy", "ExternalSecretsOperator")
        return secret

    @property
    def secret_arn(self) -> str:
        """Get the shared secret ARN"""
        return self.shared_secrets.secret_arn

    @property
    def secret_name(self) -> str:
        """Get the shared secret name"""
        return self.shared_secrets.secret_name
