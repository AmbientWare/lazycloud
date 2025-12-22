import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.components.controllers import ControllersConstruct
from infrastructure.constructs.components.infrastructure import InfrastructureConstruct
from infrastructure.stacks.shared import SharedStack


class ProdInfraStack(cdk.Stack):
    """Production infrastructure stack: VPC + EKS + Pod Identity + ConfigMap

    After deployment, install ArgoCD manually:
        helm repo add argo https://argoproj.github.io/argo-helm
        helm install argocd argo/argo-cd -n argocd --create-namespace \\
            -f infrastructure/argocd-values.yaml --wait --timeout 15m
        kubectl apply -f deploy/argocd-apps/root-app.yaml
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.shared_stack = shared_stack

        self.infrastructure = InfrastructureConstruct(
            self,
            "Infrastructure",
            config=self.config,
            shared_stack=self.shared_stack,
        )

        self.controllers = ControllersConstruct(
            self,
            "Controllers",
            config=self.config,
            shared_stack=self.shared_stack,
            eks_cluster=self.infrastructure.eks_cluster,
            vpc_id=self.infrastructure.vpc_id,
        )

        self._create_exports()

    def _create_exports(self) -> None:
        """Create CloudFormation exports for cross-stack references"""

        # VPC Exports
        cdk.CfnOutput(
            self,
            "VpcId",
            value=self.infrastructure.vpc_id,
            description="VPC ID",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-vpc-id",
        )

        cdk.CfnOutput(
            self,
            "VpcPrivateSubnets",
            value=cdk.Fn.join(
                ",",
                [
                    subnet.subnet_id
                    for subnet in self.infrastructure.vpc.private_subnets
                ],
            ),
            description="Private subnet IDs",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-vpc-private-subnets",
        )

        cdk.CfnOutput(
            self,
            "VpcPublicSubnets",
            value=cdk.Fn.join(
                ",",
                [subnet.subnet_id for subnet in self.infrastructure.vpc.public_subnets],
            ),
            description="Public subnet IDs",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-vpc-public-subnets",
        )

        # EKS Exports
        cdk.CfnOutput(
            self,
            "EksClusterName",
            value=self.infrastructure.cluster_name,
            description="EKS Cluster Name",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks-cluster-name",
        )

        cdk.CfnOutput(
            self,
            "EksClusterEndpoint",
            value=self.infrastructure.cluster_endpoint,
            description="EKS Cluster Endpoint",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks-endpoint",
        )

        cdk.CfnOutput(
            self,
            "EksClusterSecurityGroupId",
            value=self.infrastructure.eks_cluster.cluster.cluster_security_group_id,
            description="EKS Cluster Security Group ID",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks-sg-id",
        )

        cdk.CfnOutput(
            self,
            "EksOidcProviderArn",
            value=self.infrastructure.cluster_oidc_provider_arn,
            description="EKS OIDC Provider ARN",
            export_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-eks-oidc-arn",
        )

        cdk.CfnOutput(
            self,
            "KubeconfigCommand",
            value=f"aws eks update-kubeconfig --region {self.config.aws_region} --name {self.infrastructure.cluster_name}",
            description="Command to configure kubectl",
        )

        cdk.CfnOutput(
            self,
            "ArgoCDInstall",
            value="helm repo add argo https://argoproj.github.io/argo-helm && "
            "helm install argocd argo/argo-cd -n argocd --create-namespace "
            "-f infrastructure/argocd-values.yaml --wait --timeout 15m && "
            "kubectl apply -f deploy/argocd-apps/root-app.yaml",
            description="Commands to install ArgoCD after deployment",
        )
