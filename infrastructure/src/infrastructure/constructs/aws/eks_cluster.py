import json

from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import aws_eks
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk.lambda_layer_kubectl_v32 import KubectlV32Layer
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class EksCluster(Construct):
    """EKS Cluster with Karpenter autoscaling"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.Vpc,
        config: EnvironmentConfig,
        ebs_csi_role: iam.CfnRole,
        efs_csi_role: iam.CfnRole,
        load_balancer_controller_role: iam.CfnRole,
        karpenter_node_role: iam.CfnRole,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.vpc = vpc
        self.ebs_csi_role = ebs_csi_role
        self.efs_csi_role = efs_csi_role
        self.load_balancer_controller_role = load_balancer_controller_role
        self.karpenter_node_role = karpenter_node_role

        self.cluster = self._create_cluster()

    def _create_cluster(self) -> aws_eks.Cluster:
        """Create EKS cluster with managed node group for Karpenter"""
        cluster_discovery_tag = f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks"

        cluster = aws_eks.Cluster(
            self,
            "EksCluster",
            cluster_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks",
            version=aws_eks.KubernetesVersion.V1_34,
            default_capacity_type=aws_eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
            vpc=self.vpc,
            vpc_subnets=[
                ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
            ],
            endpoint_access=aws_eks.EndpointAccess.PUBLIC_AND_PRIVATE,
            kubectl_layer=KubectlV32Layer(self, "kubectl"),
            authentication_mode=aws_eks.AuthenticationMode.API_AND_CONFIG_MAP,
            tags={
                "karpenter.sh/discovery": cluster_discovery_tag,
                "Environment": self.config.environment,
                "Autoscaling": "karpenter",
                "Organization": self.config.org_name,
            },
        )

        # Add AL2023 managed node group for system components (kube-system, karpenter)
        # Following Karpenter official docs recommendations
        cluster.add_nodegroup_capacity(
            "SystemNodes",
            instance_types=[ec2.InstanceType("m5.large")],
            min_size=3,
            desired_size=3,
            max_size=10,
            ami_type=aws_eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            capacity_type=aws_eks.CapacityType.ON_DEMAND,
            labels={
                "role": "system",
                "workload-type": "system",
            },
        )

        # Add coredns add-on
        aws_eks.CfnAddon(
            self,
            "CoreDNS",
            addon_name="coredns",
            addon_version="v1.12.4-eksbuild.1",
            cluster_name=cluster.cluster_name,
        )

        # Add kube-proxy add-on
        aws_eks.CfnAddon(
            self,
            "KubeProxy",
            addon_name="kube-proxy",
            addon_version="v1.34.1-eksbuild.2",
            cluster_name=cluster.cluster_name,
        )

        # Add pod identity add-on
        aws_eks.CfnAddon(
            self,
            "PodIdentity",
            addon_name="eks-pod-identity-agent",
            addon_version="v1.3.10-eksbuild.2",
            cluster_name=cluster.cluster_name,
        )

        # Add VPC CNI add-on with custom IPAM for secondary CIDR
        # Configure VPC CNI to use secondary CIDR (100.64.0.0/16) for pod IPs
        vpc_cni_config = {
            "env": {
                "ENABLE_PREFIX_DELEGATION": "true",  # Enable prefix delegation for more pods per node
                "WARM_PREFIX_TARGET": "1",  # Keep one extra prefix warm
            }
        }

        aws_eks.CfnAddon(
            self,
            "VPCCNI",
            addon_name="vpc-cni",
            addon_version="v1.21.1-eksbuild.1",
            cluster_name=cluster.cluster_name,
            configuration_values=json.dumps(vpc_cni_config),
        )

        # Add metrics server add-on
        aws_eks.CfnAddon(
            self,
            "MetricsServer",
            addon_name="metrics-server",
            addon_version="v0.8.0-eksbuild.6",
            cluster_name=cluster.cluster_name,
        )

        # Add EBS CSI driver add-on with Pod Identity
        aws_eks.CfnAddon(
            self,
            "EBSCSI",
            addon_name="aws-ebs-csi-driver",
            addon_version="v1.54.0-eksbuild.1",
            cluster_name=cluster.cluster_name,
            pod_identity_associations=[
                aws_eks.CfnAddon.PodIdentityAssociationProperty(
                    role_arn=self.ebs_csi_role.attr_arn,
                    service_account="ebs-csi-controller-sa",
                )
            ],
        )

        # Add EFS CSI driver add-on with Pod Identity
        aws_eks.CfnAddon(
            self,
            "EFSCSI",
            addon_name="aws-efs-csi-driver",
            addon_version="v2.1.15-eksbuild.1",
            cluster_name=cluster.cluster_name,
            pod_identity_associations=[
                aws_eks.CfnAddon.PodIdentityAssociationProperty(
                    role_arn=self.efs_csi_role.attr_arn,
                    service_account="efs-csi-controller-sa",
                )
            ],
        )

        # Add Karpenter node role to aws-auth ConfigMap
        # Required for AL2023 nodes - they need both system:bootstrappers and system:nodes groups
        cluster.aws_auth.add_role_mapping(
            iam.Role.from_role_arn(
                self,
                "KarpenterNodeRoleForAuth",
                role_arn=self.karpenter_node_role.attr_arn,
            ),
            groups=["system:bootstrappers", "system:nodes"],
            username="system:node:{{EC2PrivateDNSName}}",
        )

        return cluster

    @property
    def cluster_name(self) -> str:
        """Get cluster name"""
        return (
            self.cluster.cluster_name
            or f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks"
        )

    @property
    def cluster_endpoint(self) -> str:
        """Get cluster endpoint"""
        return self.cluster.cluster_endpoint

    @property
    def cluster_certificate_authority_data(self) -> str:
        """Get cluster certificate authority data"""
        return self.cluster.cluster_certificate_authority_data

    @property
    def cluster_arn(self) -> str:
        """Get cluster ARN"""
        return self.cluster.cluster_arn

    @property
    def cluster_security_group_id(self) -> str:
        """Get cluster security group ID"""
        return self.cluster.cluster_security_group_id

    @property
    def oidc_issuer_url(self) -> str:
        """Get OIDC issuer URL"""
        return self.cluster.cluster_open_id_connect_issuer_url
