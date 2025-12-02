import aws_cdk as cdk
from aws_cdk import aws_eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws.eks_cluster import EksCluster


class LokiConstruct(Construct):
    """Deploy Loki log aggregation stack using Helm"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        config: EnvironmentConfig,
        monitoring_namespace: str = "monitoring",
        release_name: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        self.eks_cluster = eks_cluster
        self.config = config
        self.monitoring_namespace = monitoring_namespace
        self.release_name = release_name or f"loki-stack-{config.environment}"

        # Deploy Loki stack
        self.helm_chart = self._deploy_loki_helm()

    @property
    def loki_url(self) -> str:
        """Get the Loki service URL for Grafana integration"""
        return f"http://{self.release_name}:3100"

    def _get_helm_values(self) -> dict:
        """Get Helm values for Loki stack"""
        return {
            "loki": {
                "persistence": {
                    "enabled": True,
                    "storageClassName": "ebs-gp3",
                    "size": "10Gi",
                },
                "config": {
                    "query_scheduler": {"max_outstanding_requests_per_tenant": 4096},
                    "frontend": {"max_outstanding_per_tenant": 4096},
                    "query_range": {"parallelise_shardable_queries": False},
                    "limits_config": {
                        "split_queries_by_interval": "1h",
                        "max_query_parallelism": 32,
                    },
                },
            },
            "promtail": {"enabled": True},
            "grafana": {"enabled": False},  # We use the Prometheus stack's Grafana
        }

    def _deploy_loki_helm(self) -> aws_eks.HelmChart:
        """Deploy Loki stack using Helm chart"""
        return self.eks_cluster.cluster.add_helm_chart(
            "LokiStackHelmChart",
            chart="loki-stack",
            repository="https://grafana.github.io/helm-charts",
            namespace=self.monitoring_namespace,
            create_namespace=False,
            wait=True,
            timeout=cdk.Duration.minutes(10),
            values=self._get_helm_values(),
            version="2.10.2",
            release=self.release_name,
        )
