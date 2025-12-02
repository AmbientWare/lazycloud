import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import SharedSecretsConstruct
from infrastructure.stacks.shared.ecr import SharedECR
from infrastructure.stacks.shared.iam_roles import SharedIAMRoles


class SharedStack(cdk.Stack):
    """Stack for shared infrastructure across all environments"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config

        # NOTE: DNS is managed by Cloudflare, not Route53
        # ExternalDNS will automatically create DNS records in Cloudflare

        # Create IAM roles
        self.iam_roles = SharedIAMRoles(
            self,
            "IAMRoles",
            config=self.config,
        )

        # Create ECR repositories for shared services
        self.ecr = SharedECR(
            self,
            "ECR",
            config=self.config,
        )

        # Create shared secrets in AWS Secrets Manager
        # User will populate these before deploying environment stacks
        self.shared_secrets = SharedSecretsConstruct(
            self,
            "SharedSecrets",
            config=self.config,
        )

    def create_outputs(self, stack: cdk.Stack) -> None:
        """Create CloudFormation outputs for shared infrastructure"""
        # Shared Secrets Output
        cdk.CfnOutput(
            stack,
            "SharedSecretsName",
            value=self.shared_secrets.secret_name,
            description="AWS Secrets Manager secret name for shared secrets (Cloudflare API token, etc.)",
        )

        cdk.CfnOutput(
            stack,
            "SharedSecretsArn",
            value=self.shared_secrets.secret_arn,
            description="ARN of the shared secrets in AWS Secrets Manager",
        )

        # External DNS Role Output
        cdk.CfnOutput(
            stack,
            "ExternalDNSRoleArn",
            value=self.iam_roles.external_dns_role_arn,
            description="ARN of the external DNS role for EKS clusters (Cloudflare)",
        )
