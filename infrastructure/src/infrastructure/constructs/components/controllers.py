import yaml
from aws_cdk import aws_eks as eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import EksCluster
from infrastructure.stacks.shared import SharedStack


class ControllersConstruct(Construct):
    """Pod Identity Associations and Infrastructure ConfigMap for ArgoCD."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
        eks_cluster: EksCluster,
        vpc_id: str,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster
        self.shared_stack = shared_stack
        self.vpc_id = vpc_id

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

        self.argocd_namespace = self._create_argocd_namespace()
        self.infra_config = self._create_infra_configmap()

    def _create_argocd_namespace(self):
        """Create argocd namespace for ConfigMap (ArgoCD installed manually after)."""
        namespace_manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": "argocd",
            },
        }

        return self.eks_cluster.cluster.add_manifest("ArgoCDNamespace", namespace_manifest)

    def _create_infra_configmap(self):
        """Create ConfigMap with values for ArgoCD Helm charts."""
        alb_values = {
            "clusterName": self.eks_cluster.cluster_name,
            "region": self.config.aws_region,
            "vpcId": self.vpc_id,
        }

        karpenter_values = {
            "nodeRoleName": self.shared_stack.iam_roles.karpenter_node_role.role_name,
            "clusterSecurityGroupId": self.eks_cluster.cluster_security_group_id,
            "karpenter": {
                "settings": {
                    "clusterName": self.eks_cluster.cluster_name,
                    "clusterEndpoint": self.eks_cluster.cluster_endpoint,
                    "interruptionQueue": f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-karpenter",
                },
            },
        }

        external_secrets_values = {
            "orgName": self.config.org_name,
            "environment": self.config.environment,
            "region": self.config.aws_region,
        }

        ingress_values = {
            "orgName": self.config.org_name,
            "environment": self.config.environment,
            "region": self.config.aws_region,
            "ingressClasses": {
                "alb": {
                    "groupName": f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-alb",
                },
            },
        }

        common_values = {
            "clusterName": self.eks_cluster.cluster_name,
            "region": self.config.aws_region,
            "orgName": self.config.org_name,
            "environment": self.config.environment,
            "domainName": self.config.domain_name or "",
        }

        configmap_manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "infrastructure-values",
                "namespace": "argocd",
                "labels": {
                    "app.kubernetes.io/part-of": "argocd",
                },
            },
            "data": {
                "aws-load-balancer-controller.yaml": yaml.dump(
                    alb_values, default_flow_style=False
                ),
                "karpenter.yaml": yaml.dump(karpenter_values, default_flow_style=False),
                "external-secrets.yaml": yaml.dump(
                    external_secrets_values, default_flow_style=False
                ),
                "ingress-classes.yaml": yaml.dump(
                    ingress_values, default_flow_style=False
                ),
                "common.yaml": yaml.dump(common_values, default_flow_style=False),
            },
        }

        configmap = self.eks_cluster.cluster.add_manifest(
            "InfrastructureConfigMap", configmap_manifest
        )
        configmap.node.add_dependency(self.argocd_namespace)

        return configmap
