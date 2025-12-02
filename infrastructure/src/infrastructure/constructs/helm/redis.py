import aws_cdk as cdk
from aws_cdk import aws_eks
from constructs import Construct

from infrastructure.constructs.aws.eks_cluster import EksCluster
from infrastructure.constructs.helm.namespace import KubernetesNamespace


class RedisHelmChart(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        eks_cluster: EksCluster,
        namespace: str = "redis",
        values: dict | None = None,
    ):
        super().__init__(scope, id)
        self._namespace_name = namespace

        # Save inputs
        self.eks_cluster = eks_cluster
        self._helm_values = values or {
            "architecture": "standalone",
            "auth": {
                "enabled": False
            },  # NOTE: For dev only! Set to True for prod by passing in values
        }

        # Create namespace and deploy Redis Helm chart
        self._create_namespace()
        self.helm_chart = self._deploy_redis_helm()

    def _create_namespace(self) -> None:
        """Create Redis namespace"""
        self.namespace = KubernetesNamespace(
            self,
            "RedisNamespace",
            eks_cluster=self.eks_cluster,
            name=self._namespace_name or "redis",
        )

    def _get_helm_values(self) -> dict:
        """Get Helm values for Redis chart"""
        return self._helm_values

    def _deploy_redis_helm(self) -> aws_eks.HelmChart:
        """Deploy Redis using Helm chart"""
        return self.eks_cluster.cluster.add_helm_chart(
            "RedisHelmChart",
            chart="redis",
            repository="https://charts.bitnami.com/bitnami",
            namespace=self.namespace.name,
            create_namespace=False,
            wait=True,
            timeout=cdk.Duration.minutes(10),
            values=self._get_helm_values(),
        )
