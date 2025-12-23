import aws_cdk as cdk
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class SharedSecretsConstruct(Construct):
    """Shared secrets for all environments (Cloudflare API token, etc.)"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        self.shared_secrets = secretsmanager.Secret(
            self,
            "SharedSecrets",
            secret_name=f"{config.org_name}/shared-secrets",
            description=f"Shared secrets (Cloudflare, etc.) for {config.org_name}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="__placeholder__",
            ),
        )
        cdk.Tags.of(self.shared_secrets).add("Environment", "shared")
        cdk.Tags.of(self.shared_secrets).add("Organization", config.org_name)
        cdk.Tags.of(self.shared_secrets).add("ManagedBy", "ExternalSecretsOperator")

    @property
    def secret_arn(self) -> str:
        return self.shared_secrets.secret_arn

    @property
    def secret_name(self) -> str:
        return self.shared_secrets.secret_name


class EnvironmentSecretsConstruct(Construct):
    """App secrets for each environment (namespace) this cluster hosts.

    Creates secrets based on config.app_environments list.
    Populate via AWS CLI:
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
        self.secrets: dict[str, secretsmanager.Secret] = {}

        # Create a secret for each app environment this cluster hosts
        app_envs = config.app_environments or [config.environment]
        for app_env in app_envs:
            secret = secretsmanager.Secret(
                self,
                f"AppSecrets-{app_env}",
                secret_name=f"{config.org_name}/{app_env}-secrets",
                description=f"App secrets for {config.org_name} - synced to lazycloud-{app_env} namespace",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template="{}",
                    generate_string_key="__placeholder__",
                ),
            )
            cdk.Tags.of(secret).add("Environment", app_env)
            cdk.Tags.of(secret).add("Organization", config.org_name)
            cdk.Tags.of(secret).add("ManagedBy", "ExternalSecretsOperator")
            self.secrets[app_env] = secret

    def get_secret(self, app_env: str) -> secretsmanager.Secret:
        """Get secret for a specific app environment"""
        return self.secrets[app_env]
