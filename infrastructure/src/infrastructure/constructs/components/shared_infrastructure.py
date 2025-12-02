import aws_cdk as cdk
from aws_cdk import aws_eks as eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import EksCluster, VpcConstruct
from infrastructure.constructs.helm import (
    AWSLoadBalancerControllerConstruct,
    KarpenterConstruct,
)
from infrastructure.stacks.shared import SharedStack


class SharedInfrastructure(Construct):
    """Shared infrastructure components like VPC and EKS cluster

    This component is used by both dev and production stacks.
    Environment-specific behavior is controlled via the EnvironmentConfig.
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

        # Get shared secrets ARN (contains Cloudflare API token and other secrets)
        shared_secrets_arn = (
            shared_stack.shared_secrets.secret_arn
            if hasattr(shared_stack, "shared_secrets")
            else None
        )

        # Create EKS Cluster
        self.eks_cluster = EksCluster(
            self,
            "EksCluster",
            vpc=self.vpc_construct.vpc,
            config=self.config,
            external_dns_role=shared_stack.iam_roles.external_dns_role,
            ebs_csi_role=shared_stack.iam_roles.ebs_csi_role,
            efs_csi_role=shared_stack.iam_roles.efs_csi_role,
            load_balancer_controller_role=shared_stack.iam_roles.load_balancer_controller_role,
            karpenter_node_role=shared_stack.iam_roles.karpenter_node_role,
            cloudflare_secret_arn=shared_secrets_arn,  # Reference to shared-secrets
        )

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
            vpc=self.vpc_construct.vpc,
            config=self.config,
            karpenter_controller_role=shared_stack.iam_roles.karpenter_controller_role,
            karpenter_node_role=shared_stack.iam_roles.karpenter_node_role,
        )
        self.karpenter.node.add_dependency(self.eks_cluster.cluster)
        self.karpenter.node.add_dependency(self.load_balancer_controller.chart)
        self.karpenter.node.add_dependency(self.karpenter_pod_identity)

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

    def create_outputs(self, stack: cdk.Stack) -> None:
        """Create CloudFormation outputs for shared infrastructure"""
        # VPC Outputs
        cdk.CfnOutput(
            stack,
            "VpcId",
            value=self.vpc_id,
            description="VPC ID",
        )

        # EKS Outputs
        cdk.CfnOutput(
            stack,
            "EksClusterName",
            value=self.cluster_name,
            description="EKS Cluster Name",
        )

        cdk.CfnOutput(
            stack,
            "EksClusterEndpoint",
            value=self.cluster_endpoint,
            description="EKS Cluster Endpoint",
        )

        # Kubeconfig command
        cdk.CfnOutput(
            stack,
            "KubeconfigCommand",
            value=f"aws eks update-kubeconfig --region {self.config.aws_region} --name {self.cluster_name}",
            description="Command to configure kubectl",
        )
