import aws_cdk as cdk
from aws_cdk import aws_eks as eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import EksCluster
from infrastructure.constructs.helm import (
    AWSLoadBalancerControllerConstruct,
    KarpenterConstruct,
)
from infrastructure.stacks.shared import SharedStack


class ControllersConstruct(Construct):
    """AWS controllers: Karpenter, ALB Controller, EBS CSI

    This construct contains AWS-specific controllers that:
    - Manage cluster autoscaling (Karpenter)
    - Manage load balancers (AWS Load Balancer Controller)
    - Are updated monthly/quarterly
    - Should not cause EKS cluster rollback if they fail
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
        eks_cluster: EksCluster,
        vpc_id: str,
        vpc,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster

        # Install AWS Load Balancer Controller via Helm
        self.load_balancer_controller = AWSLoadBalancerControllerConstruct(
            self,
            "LoadBalancerController",
            eks_cluster=self.eks_cluster,
            config=self.config,
            service_account_role_arn=shared_stack.iam_roles.load_balancer_controller_role_arn,
        )
        self.load_balancer_controller.node.add_dependency(self.eks_cluster.cluster)

        # Create Pod Identity Association for Karpenter BEFORE deploying Karpenter
        # This ensures the service account has IAM permissions when pods start
        # Karpenter MUST be in kube-system namespace (webhooks have it hard-coded)
        self.karpenter_pod_identity = eks.CfnPodIdentityAssociation(
            self,
            "KarpenterPodIdentity",
            cluster_name=self.eks_cluster.cluster_name,
            namespace="kube-system",
            service_account="karpenter",
            role_arn=shared_stack.iam_roles.karpenter_controller_role.attr_arn,
        )
        self.karpenter_pod_identity.node.add_dependency(self.eks_cluster.cluster)

        # Deploy Karpenter after cluster is ready
        # NOTE: We pass subnet IDs explicitly to avoid circular dependency from tagging
        self.karpenter = KarpenterConstruct(
            self,
            "Karpenter",
            eks_cluster=self.eks_cluster,
            vpc=vpc,
            config=self.config,
            karpenter_controller_role=shared_stack.iam_roles.karpenter_controller_role,
            karpenter_node_role=shared_stack.iam_roles.karpenter_node_role,
        )
        self.karpenter.node.add_dependency(self.eks_cluster.cluster)
        self.karpenter.node.add_dependency(self.load_balancer_controller.chart)
        self.karpenter.node.add_dependency(self.karpenter_pod_identity)
