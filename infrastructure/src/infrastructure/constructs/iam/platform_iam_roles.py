import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class PlatformIAMRoles(Construct):
    """IAM roles for platform services (External DNS, External Secrets)"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create platform IAM roles
        self.external_dns_role = self._create_external_dns_role()
        self.external_secrets_role = self._create_external_secrets_role()
        self.ecr_base_role = self._create_ecr_base_role()

    def _create_external_dns_role(self) -> iam.CfnRole:
        """
        Create IAM role for external-dns with Cloudflare API token access
        Uses AWS Secrets Manager to store the Cloudflare API token
        """

        # Trust policy for EKS Pod Identity
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "pods.eks.amazonaws.com"},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                }
            ],
        }

        # Permissions policy - access to Secrets Manager for Cloudflare API token
        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    "Resource": f"arn:aws:secretsmanager:*:{cdk.Aws.ACCOUNT_ID}:secret:{self.config.org_name}/*",
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "ExternalDNSRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="ExternalDNSPolicy", policy_document=permissions_policy
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "ExternalDNS")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    def _create_external_secrets_role(self) -> iam.CfnRole:
        """
        Create IAM role for external-secrets with AWS Secrets Manager permissions
        Based on External Secrets Operator documentation: https://external-secrets.io/latest/provider/aws-secrets-manager/
        """

        # Trust policy for EKS Pod Identity
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "pods.eks.amazonaws.com"},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                }
            ],
        }

        # Permissions policy based on External Secrets documentation
        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "secretsmanager:ListSecrets",
                        "secretsmanager:BatchGetSecretValue",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "secretsmanager:GetResourcePolicy",
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                        "secretsmanager:ListSecretVersionIds",
                    ],
                    "Resource": [
                        f"arn:aws:secretsmanager:*:{cdk.Aws.ACCOUNT_ID}:secret:{self.config.org_name}/*"
                    ],
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "ExternalSecretsRole",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="ExternalSecretsPolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "ExternalSecrets")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    def _create_ecr_base_role(self) -> iam.CfnRole:
        """
        Create IAM role for ECR operations.
        This role is assumed by the backend API with a session policy
        that scopes access to specific tenant repositories.
        """

        # Trust policy - allows the account to assume this role
        # The backend uses STS AssumeRole with a session policy to scope access
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{cdk.Aws.ACCOUNT_ID}:root"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        # Full ECR permissions - scoped down at assume time via session policy
        permissions_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:GetAuthorizationToken",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:CreateRepository",
                        "ecr:DeleteRepository",
                        "ecr:DescribeRepositories",
                        "ecr:ListImages",
                        "ecr:DescribeImages",
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:InitiateLayerUpload",
                        "ecr:UploadLayerPart",
                        "ecr:CompleteLayerUpload",
                        "ecr:PutImage",
                        "ecr:PutLifecyclePolicy",
                        "ecr:GetLifecyclePolicy",
                        "ecr:TagResource",
                    ],
                    "Resource": f"arn:aws:ecr:*:{cdk.Aws.ACCOUNT_ID}:repository/*",
                },
            ],
        }

        role = iam.CfnRole(
            self,
            "ECRBaseRole",
            role_name=f"{self.config.org_name}-ecr-base-role",
            assume_role_policy_document=trust_policy,
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="ECRBasePolicy",
                    policy_document=permissions_policy,
                )
            ],
        )

        # Add tags
        cdk.Tags.of(role).add("Purpose", "ECRBaseRole")
        cdk.Tags.of(role).add("Environment", "shared")
        cdk.Tags.of(role).add("Organization", self.config.org_name)

        return role

    @property
    def external_dns_role_arn(self) -> str:
        """Get the external DNS role ARN"""
        return self.external_dns_role.attr_arn

    @property
    def external_secrets_role_arn(self) -> str:
        """Get the external secrets role ARN"""
        return self.external_secrets_role.attr_arn

    @property
    def ecr_base_role_arn(self) -> str:
        """Get the ECR base role ARN"""
        return self.ecr_base_role.attr_arn
