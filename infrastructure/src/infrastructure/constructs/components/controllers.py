from aws_cdk import aws_eks as eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import EksCluster
from infrastructure.stacks.shared import SharedStack


class ControllersConstruct(Construct):
    """Pod Identity Associations for platform controllers."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
        eks_cluster: EksCluster,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster
        self.shared_stack = shared_stack

        self.karpenter_pod_identity = eks.CfnPodIdentityAssociation(
            self,
            "KarpenterPodIdentity",
            cluster_name=self.eks_cluster.cluster_name,
            namespace="kube-system",
            service_account="karpenter",
            role_arn=shared_stack.iam_roles.karpenter_controller_role.attr_arn,
        )

        self.external_secrets_pod_identity = eks.CfnPodIdentityAssociation(
            self,
            "ExternalSecretsPodIdentity",
            cluster_name=self.eks_cluster.cluster_name,
            namespace="external-secrets-system",
            service_account="external-secrets",
            role_arn=shared_stack.iam_roles.external_secrets_role_arn,
        )

        self.alb_controller_pod_identity = eks.CfnPodIdentityAssociation(
            self,
            "ALBControllerPodIdentity",
            cluster_name=self.eks_cluster.cluster_name,
            namespace="kube-system",
            service_account="aws-load-balancer-controller",
            role_arn=shared_stack.iam_roles.load_balancer_controller_role_arn,
        )
