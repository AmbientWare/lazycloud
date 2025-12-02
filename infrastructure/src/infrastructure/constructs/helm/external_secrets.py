from typing import Any

import aws_cdk as cdk
import aws_cdk.aws_secretsmanager as secretsmanager
from aws_cdk import aws_eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws.eks_cluster import EksCluster
from infrastructure.constructs.helm.namespace import KubernetesNamespace


class ExternalSecretsConstruct(Construct):
    """External Secrets Operator deployment on EKS using Helm with AWS Secrets Manager integration"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        config: EnvironmentConfig,
        external_secrets_role_arn: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster
        self.external_secrets_role_arn = external_secrets_role_arn

        # Create External Secrets namespace
        self._create_namespace()

        # Deploy External Secrets Operator via Helm
        self.helm_chart = self._deploy_external_secrets_helm()

        # Create Pod Identity association for External Secrets
        self._create_pod_identity_association()

        # Initialize secret_store as None - will be created when needed
        self.secret_store = None

    def _create_namespace(self) -> None:
        """Create External Secrets namespace"""
        self.namespace = KubernetesNamespace(
            self,
            "ExternalSecretsNamespace",
            eks_cluster=self.eks_cluster,
            name="external-secrets-system",
        )

    def _deploy_external_secrets_helm(self) -> aws_eks.HelmChart:
        """Deploy External Secrets Operator using Helm chart"""
        return self.eks_cluster.cluster.add_helm_chart(
            "ExternalSecrets",
            chart="external-secrets",
            repository="https://charts.external-secrets.io",
            namespace=self.namespace.name,
            version="0.18.0",
            wait=True,
            timeout=cdk.Duration.minutes(15),
            values=self._get_helm_values(),
        )

    def _get_helm_values(self) -> dict[str, Any]:
        """Get Helm values for External Secrets deployment"""
        return {
            # Override default names to avoid CDK's long auto-generated names
            "nameOverride": "external-secrets",
            "fullnameOverride": "external-secrets",
            # Install CRDs
            "installCRDs": True,
            # Service account without IRSA annotations (using Pod Identity)
            "serviceAccount": {
                "create": True,
                "name": "external-secrets",
            },
            # Basic resource limits
            "resources": {
                "limits": {
                    "cpu": "200m",
                    "memory": "256Mi",
                },
                "requests": {
                    "cpu": "100m",
                    "memory": "128Mi",
                },
            },
        }

    def _create_pod_identity_association(self) -> None:
        """Create Pod Identity association for External Secrets"""
        self.pod_identity_association = aws_eks.CfnPodIdentityAssociation(
            self,
            "ExternalSecretsPodIdentity",
            cluster_name=self.eks_cluster.cluster_name,
            namespace=self.namespace.name,
            service_account="external-secrets",
            role_arn=self.external_secrets_role_arn,
        )

        # Ensure Pod Identity association is created after the Helm chart
        self.pod_identity_association.node.add_dependency(self.helm_chart)

    def create_secret_store(self) -> None:
        """Create ClusterSecretStore for AWS Secrets Manager"""
        if self.secret_store is not None:
            return

        secret_store_manifest = {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ClusterSecretStore",
            "metadata": {
                "name": f"{self.config.org_name}-{self.config.environment}-secrets",
            },
            "spec": {
                "provider": {
                    "aws": {
                        "service": "SecretsManager",
                        "region": self.config.aws_region,
                    }
                }
            },
        }

        self.secret_store = self.eks_cluster.cluster.add_manifest(
            "ClusterSecretStore",
            secret_store_manifest,
        )

        # Ensure the secret store is created after the Helm chart
        self.secret_store.node.add_dependency(self.helm_chart)

    def create_aws_secret(
        self,
        secret_name: str,
        secret_value: dict[str, Any],
        description: str | None = None,
    ) -> secretsmanager.Secret:
        """Create an AWS Secrets Manager secret"""
        full_secret_name = (
            f"{self.config.org_name}/{self.config.environment}/{secret_name}"
        )

        final_description = (
            description
            if description is not None
            else f"{secret_name} for {self.config.org_name} {self.config.environment} environment"
        )

        secret = secretsmanager.Secret(
            self,
            f"Secret-{secret_name}",
            secret_name=full_secret_name,
            description=final_description,
            secret_object_value={
                key: cdk.SecretValue.unsafe_plain_text(str(value))
                for key, value in secret_value.items()
            },
        )

        return secret

    def create_external_secret(
        self, target_namespace: str, secret_name: str, aws_secret_name: str
    ):
        """Create an ExternalSecret that syncs from AWS Secrets Manager to Kubernetes"""
        full_aws_secret_name = (
            f"{self.config.org_name}/{self.config.environment}/{aws_secret_name}"
        )

        external_secret_manifest = {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {
                "name": f"{secret_name}-external",
                "namespace": target_namespace,
            },
            "spec": {
                "refreshInterval": "15m",
                "secretStoreRef": {
                    "name": f"{self.config.org_name}-{self.config.environment}-secrets",
                    "kind": "ClusterSecretStore",
                },
                "target": {
                    "name": secret_name,
                    "creationPolicy": "Owner",
                },
                "dataFrom": [
                    {
                        "extract": {
                            "key": full_aws_secret_name,
                        }
                    }
                ],
            },
        }

        external_secret = self.eks_cluster.cluster.add_manifest(
            f"ExternalSecret-{secret_name}",
            external_secret_manifest,
        )

        # Ensure the external secret is created after the Helm chart (for CRDs)
        external_secret.node.add_dependency(self.helm_chart)

        # Ensure the external secret is created after the cluster secret store
        if self.secret_store:
            external_secret.node.add_dependency(self.secret_store)

        return external_secret

    @property
    def external_secrets_namespace(self) -> str:
        """Get External Secrets namespace"""
        return self.namespace.name

    @property
    def secret_store_name(self) -> str:
        """Get SecretStore name"""
        return f"{self.config.org_name}-{self.config.environment}-secrets"

    @property
    def service_account_role_arn(self) -> str:
        """Get the IAM role ARN for the service account"""
        return self.external_secrets_role_arn
