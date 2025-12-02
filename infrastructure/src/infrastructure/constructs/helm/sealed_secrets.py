from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws.eks_cluster import EksCluster
from infrastructure.constructs.helm.namespace import KubernetesNamespace


class SealedSecretsConstruct(Construct):
    """Sealed Secrets deployment on EKS using Helm"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster

        # Create Sealed Secrets namespace
        self._create_namespace()

        # Deploy Sealed Secrets via Helm
        self.helm_chart = self._deploy_sealed_secrets_helm()

    def _create_namespace(self) -> None:
        """Create Sealed Secrets namespace"""
        self.namespace = KubernetesNamespace(
            self,
            "SealedSecretsNamespace",
            eks_cluster=self.eks_cluster,
            name="sealed-secrets",
        )

    def _deploy_sealed_secrets_helm(self) -> aws_eks.HelmChart:
        """Deploy Sealed Secrets using Helm chart"""
        return self.eks_cluster.cluster.add_helm_chart(
            "SealedSecrets",
            chart="sealed-secrets",
            repository="https://bitnami-labs.github.io/sealed-secrets",
            namespace=self.namespace.name,
            version="2.2.0",  # Latest stable version
            wait=True,
            timeout=cdk.Duration.minutes(10),
            values=self._get_helm_values(),
        )

    def _get_helm_values(self) -> dict[str, Any]:
        """Get Helm values for Sealed Secrets deployment"""
        values = {
            # Override default names to avoid CDK's long auto-generated names
            "nameOverride": "sealed-secrets",
            "fullnameOverride": "sealed-secrets",
            # Controller configuration
            "controller": {
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
                "metrics": {
                    "enabled": True,
                    "serviceMonitor": {
                        "enabled": True,
                    },
                },
                "replicas": 1,
                "updateStrategy": {
                    "type": "RollingUpdate",
                },
            },
            # Security context
            "securityContext": {
                "enabled": True,
                "fsGroup": 1001,
                "runAsUser": 1001,
            },
            # Service account configuration
            "serviceAccount": {
                "create": True,
                "name": "sealed-secrets",
            },
            # Pod security context
            "podSecurityContext": {
                "enabled": True,
                "fsGroup": 1001,
            },
            # Service configuration
            "service": {
                "type": "ClusterIP",
                "port": 8080,
            },
            # Prometheus metrics configuration
            "prometheus": {
                "enabled": True,
                "serviceMonitor": {
                    "enabled": True,
                },
            },
        }

        return values

    @property
    def sealed_secrets_namespace(self) -> str:
        """Get Sealed Secrets namespace"""
        return self.namespace.name

    @property
    def controller_name(self) -> str:
        """Get Sealed Secrets controller name"""
        return "sealed-secrets-controller"

    @property
    def setup_instructions(self) -> str:
        """Get setup instructions"""
        return f"""
# Sealed Secrets Setup Instructions:

1. Verify the controller is running:
   kubectl get pods -n {self.namespace.name}

2. Get the public key for encryption:
   kubectl get secret -n {self.namespace.name} -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml

3. Install the kubeseal CLI tool:
   # For Linux:
   wget https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.24.1/kubeseal-0.24.1-linux-amd64.tar.gz
   tar -xzf kubeseal-0.24.1-linux-amd64.tar.gz
   sudo mv kubeseal /usr/local/bin/

4. Use kubeseal to encrypt secrets:
   kubeseal --fetch-cert > pub-cert.pem
   kubeseal --cert pub-cert.pem -f secret.yaml > sealed-secret.yaml
"""
