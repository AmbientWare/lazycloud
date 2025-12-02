import aws_cdk as cdk
from aws_cdk import aws_eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws.eks_cluster import EksCluster


class PrometheusConstruct(Construct):
    """Deploy Prometheus monitoring stack using Helm"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        config: EnvironmentConfig,
        monitoring_namespace: str = "monitoring",
        grafana_hostname: str | None = None,
        loki_url: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        self.eks_cluster = eks_cluster
        self.config = config
        self.monitoring_namespace = monitoring_namespace
        self.grafana_hostname = grafana_hostname or f"grafana.{config.org_name}.com"
        self.loki_url = loki_url

        # Deploy Prometheus stack
        self.helm_chart = self._deploy_prometheus_helm()

    def _get_helm_values(self) -> dict:
        """Get Helm values for Prometheus stack"""
        values = {
            "prometheus-node-exporter": {"hostRootFsMount": {"enabled": False}},
            "prometheusOperator": {"admissionWebhooks": {"failurePolicy": "Ignore"}},
            "prometheus": {
                "prometheusSpec": {
                    "retention": "30d",
                    "storageSpec": {
                        "volumeClaimTemplate": {
                            "spec": {
                                "storageClassName": "ebs-gp3",
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "50Gi"}},
                            }
                        }
                    },
                }
            },
            "grafana": {
                "adminPassword": f"{self.config.org_name.capitalize()}Password",
                "persistence": {
                    "enabled": True,
                    "storageClassName": "ebs-gp3",
                    "size": "10Gi",
                },
                "service": {"type": "ClusterIP"},
                "ingress": {
                    "enabled": True,
                    "ingressClassName": "alb",
                    "annotations": {
                        "alb.ingress.kubernetes.io/scheme": "internet-facing",
                        "alb.ingress.kubernetes.io/target-type": "ip",
                        "external-dns.alpha.kubernetes.io/hostname": self.grafana_hostname,
                    },
                    "hosts": [self.grafana_hostname.replace(".com", ".dev")],
                },
            },
            "alertmanager": {
                "alertmanagerSpec": {
                    "storage": {
                        "volumeClaimTemplate": {
                            "spec": {
                                "storageClassName": "ebs-gp3",
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": "10Gi"}},
                            }
                        }
                    }
                }
            },
        }

        # Add Loki data source if URL is provided
        if self.loki_url:
            values["grafana"]["additionalDataSources"] = [
                {
                    "name": "Loki",
                    "type": "loki",
                    "url": self.loki_url,
                    "access": "proxy",
                    "isDefault": False,
                    "jsonData": {"maxLines": 1000},
                }
            ]

        return values

    def _deploy_prometheus_helm(self) -> aws_eks.HelmChart:
        """Deploy Prometheus stack using Helm chart"""
        return self.eks_cluster.cluster.add_helm_chart(
            "PrometheusStackHelmChart",
            chart="kube-prometheus-stack",
            repository="https://prometheus-community.github.io/helm-charts",
            namespace=self.monitoring_namespace,
            create_namespace=False,
            wait=True,
            timeout=cdk.Duration.minutes(15),
            values=self._get_helm_values(),
            version="66.3.0",
        )
