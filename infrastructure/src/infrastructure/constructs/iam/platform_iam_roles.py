import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class PlatformIAMRoles(Construct):
    """IAM roles for platform services (External Secrets, ECR)"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create platform IAM roles
        self.external_secrets_role = self._create_external_secrets_role()
        self.ecr_base_role = self._create_ecr_base_role()
        self.backend_service_user = self._create_backend_service_user()
        self.backend_service_access_key = self._create_backend_service_access_key()
        self.platform_credentials_secret = self._create_platform_credentials_secret()

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

    def _create_backend_service_user(self) -> iam.CfnUser:
        """
        Create IAM user for the backend API service.
        This user can assume the ECR base role and manage ECR repositories.
        """
        # Policy to assume the ECR base role
        assume_role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Resource": self.ecr_base_role.attr_arn,
                }
            ],
        }

        # Direct ECR permissions for repo management (before assuming role)
        ecr_management_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:GetAuthorizationToken",
                        "ecr:DescribeRepositories",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:CreateRepository",
                        "ecr:DeleteRepository",
                        "ecr:DescribeImages",
                        "ecr:ListImages",
                        "ecr:PutLifecyclePolicy",
                        "ecr:TagResource",
                    ],
                    "Resource": f"arn:aws:ecr:*:{cdk.Aws.ACCOUNT_ID}:repository/lc-*",
                },
            ],
        }

        user = iam.CfnUser(
            self,
            "BackendServiceUser",
            user_name=f"{self.config.org_name}-backend-service",
            policies=[
                iam.CfnUser.PolicyProperty(
                    policy_name="AssumeECRBaseRole",
                    policy_document=assume_role_policy,
                ),
                iam.CfnUser.PolicyProperty(
                    policy_name="ECRManagement",
                    policy_document=ecr_management_policy,
                ),
            ],
        )

        # Add tags
        cdk.Tags.of(user).add("Purpose", "BackendService")
        cdk.Tags.of(user).add("Environment", "shared")
        cdk.Tags.of(user).add("Organization", self.config.org_name)

        return user

    def _create_backend_service_access_key(self) -> iam.CfnAccessKey:
        """
        Create access key for the backend service user.
        """
        access_key = iam.CfnAccessKey(
            self,
            "BackendServiceAccessKey",
            user_name=self.backend_service_user.user_name,
        )
        return access_key

    def _create_platform_credentials_secret(self) -> secretsmanager.CfnSecret:
        """
        Create a secret to store the backend service credentials.
        These are used by the backend API for ECR operations.
        """
        secret = secretsmanager.CfnSecret(
            self,
            "PlatformCredentialsSecret",
            name=f"{self.config.org_name}/platform-credentials",
            description=f"Platform credentials for {self.config.org_name} backend services",
            secret_string=cdk.Fn.sub(
                '{"AWS_ACCESS_KEY_ID":"${AccessKeyId}","AWS_SECRET_ACCESS_KEY":"${SecretAccessKey}"}',
                {
                    "AccessKeyId": self.backend_service_access_key.ref,
                    "SecretAccessKey": self.backend_service_access_key.attr_secret_access_key,
                },
            ),
        )

        # Add tags
        cdk.Tags.of(secret).add("Purpose", "PlatformCredentials")
        cdk.Tags.of(secret).add("Environment", "shared")
        cdk.Tags.of(secret).add("Organization", self.config.org_name)

        return secret

    @property
    def external_secrets_role_arn(self) -> str:
        """Get the external secrets role ARN"""
        return self.external_secrets_role.attr_arn

    @property
    def ecr_base_role_arn(self) -> str:
        """Get the ECR base role ARN"""
        return self.ecr_base_role.attr_arn

    @property
    def backend_service_user_arn(self) -> str:
        """Get the backend service user ARN"""
        return self.backend_service_user.attr_arn

    @property
    def platform_credentials_secret_arn(self) -> str:
        """Get the platform credentials secret ARN"""
        return self.platform_credentials_secret.ref
