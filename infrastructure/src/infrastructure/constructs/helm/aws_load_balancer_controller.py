import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class AWSLoadBalancerControllerConstruct(Construct):
    """AWS Load Balancer Controller Helm chart"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster,
        config: EnvironmentConfig,
        service_account_role_arn: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster
        self.service_account_role_arn = service_account_role_arn

        # Create service account for AWS Load Balancer Controller
        self.service_account = self._create_service_account()

        # Install AWS Load Balancer Controller Helm chart
        self.chart = self._install_helm_chart()

    def _create_service_account(self):
        """Create Kubernetes service account for AWS Load Balancer Controller"""
        service_account_name = "aws-load-balancer-controller"
        namespace = "kube-system"

        service_account = self.eks_cluster.cluster.add_manifest(
            "LoadBalancerControllerServiceAccount",
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": service_account_name,
                    "namespace": namespace,
                    "annotations": {
                        "eks.amazonaws.com/role-arn": self.service_account_role_arn,
                    },
                },
            },
        )

        return service_account

    def _install_helm_chart(self):
        """Install AWS Load Balancer Controller Helm chart"""
        cluster_name = self.eks_cluster.cluster_name

        chart = self.eks_cluster.cluster.add_helm_chart(
            "LoadBalancerController",
            chart="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace="kube-system",
            version="1.14.0",
            wait=True,
            timeout=cdk.Duration.minutes(10),
            values={
                "clusterName": cluster_name,
                "serviceAccount": {
                    "create": False,
                    "name": "aws-load-balancer-controller",
                },
                "region": self.config.aws_region,
                "vpcId": self.eks_cluster.vpc.vpc_id,
            },
        )

        # Ensure chart is installed after service account
        chart.node.add_dependency(self.service_account)

        return chart
