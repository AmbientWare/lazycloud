import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import EksCluster, VpcConstruct
from infrastructure.stacks.shared import SharedStack


class InfrastructureConstruct(Construct):
    """Core infrastructure components: VPC and EKS cluster

    This construct contains the stable foundation that rarely changes:
    - VPC with public/private subnets
    - EKS cluster control plane
    - EKS managed node group (for system pods)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create VPC
        self.vpc_construct = VpcConstruct(
            self,
            "VpcConstruct",
            config=self.config,
        )

        # Create EKS Cluster
        self.eks_cluster = EksCluster(
            self,
            "EksCluster",
            vpc=self.vpc_construct.vpc,
            config=self.config,
            ebs_csi_role=shared_stack.iam_roles.ebs_csi_role,
            efs_csi_role=shared_stack.iam_roles.efs_csi_role,
            load_balancer_controller_role=shared_stack.iam_roles.load_balancer_controller_role,
            karpenter_node_role=shared_stack.iam_roles.karpenter_node_role,
        )

    @property
    def vpc(self):
        """Get the VPC instance"""
        return self.vpc_construct.vpc

    @property
    def vpc_id(self):
        """Get the VPC ID"""
        return self.vpc_construct.vpc_id

    @property
    def cluster_name(self):
        """Get the EKS cluster name"""
        return self.eks_cluster.cluster_name

    @property
    def cluster_endpoint(self):
        """Get the EKS cluster endpoint"""
        return self.eks_cluster.cluster_endpoint

    @property
    def cluster_oidc_provider_arn(self):
        """Get the EKS cluster OIDC provider ARN"""
        return self.eks_cluster.cluster.open_id_connect_provider.open_id_connect_provider_arn
