import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.helm import (
    ArgoCDConstruct,
    ExternalSecretsConstruct,
    KubernetesNamespace,
    PrometheusConstruct,
    LokiConstruct,
)
from infrastructure.constructs.kubernetes import IngressClass, StorageClass


class PlatformServices(Construct):
    """Platform services like ArgoCD and other Helm charts we use outside of our applications"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        eks_cluster,
        external_secrets_role_arn: str,
        load_balancer_controller,
        shared_secrets_name: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster
        self.load_balancer_controller = load_balancer_controller
        self.shared_secrets_name = shared_secrets_name

        # Create ArgoCD (depends on EKS cluster)
        self.argocd = ArgoCDConstruct(
            self,
            "ArgoCD",
            eks_cluster=self.eks_cluster,
            config=self.config,
        )
        # ArgoCD ingress depends on Load Balancer Controller
        if self.argocd.ingress:
            self.argocd.ingress.node.add_dependency(self.load_balancer_controller.chart)

        # create external secrets
        self.external_secrets = ExternalSecretsConstruct(
            self,
            "ExternalSecrets",
            eks_cluster=self.eks_cluster,
            config=self.config,
            external_secrets_role_arn=external_secrets_role_arn,
        )

        # Create ExternalSecret to sync shared secrets to kube-system namespace
        # This includes Cloudflare API token for ExternalDNS
        if self.shared_secrets_name:
            self._create_shared_secrets_sync()

        # create monitoring namespace
        self.monitoring_namespace = KubernetesNamespace(
            self,
            "MonitoringNamespace",
            eks_cluster=self.eks_cluster,
            name="monitoring",
        )

        # Deploy Loki for log aggregation
        self.loki = LokiConstruct(
            self,
            "Loki",
            eks_cluster=self.eks_cluster,
            config=self.config,
            monitoring_namespace=self.monitoring_namespace.name,
        )

        # Deploy Prometheus monitoring stack with Grafana
        self.prometheus = PrometheusConstruct(
            self,
            "Prometheus",
            eks_cluster=self.eks_cluster,
            config=self.config,
            monitoring_namespace=self.monitoring_namespace.name,
            loki_url=self.loki.loki_url,
        )

        ## PRODUCTION STUFF

        # create namespace for the environment stack
        self.namespace = KubernetesNamespace(
            self,
            f"{self.config.environment}Namespace",
            eks_cluster=self.eks_cluster,
            name=f"{self.config.org_name}-{self.config.environment}",
        )

        # Create AWS Secrets Manager secret with application configuration
        self.app_secret = self.external_secrets.create_aws_secret(
            secret_name="app-secrets",
            secret_value={},
            description=f"Application secrets for {self.config.org_name} {self.config.environment} environment",
        )

        # Create ExternalSecret to sync the AWS secret to Kubernetes
        self.app_external_secret = self.external_secrets.create_external_secret(
            target_namespace=self.namespace.name,
            secret_name="app-secrets",
            aws_secret_name="app-secrets",
        )
        # Add dependency to ensure namespace exists before creating external secret
        self.app_external_secret.node.add_dependency(self.namespace)

        # Create the SecretStore after the namespace is created
        self.external_secrets.create_secret_store()

        ## STAGING STUFF
        # TODO: remove all of this when we have deadicated environment clusters

        self.staging_namespace = KubernetesNamespace(
            self,
            f"{self.config.environment}StagingNamespace",
            eks_cluster=self.eks_cluster,
            name=f"{self.config.org_name}-{self.config.environment}-staging",
        )

        self.app_secret_staging = self.external_secrets.create_aws_secret(
            secret_name="app-secrets-staging",
            secret_value={},
            description=f"Application secrets for {self.config.org_name} {self.config.environment} staging environment",
        )

        self.app_external_secret_staging = self.external_secrets.create_external_secret(
            target_namespace=self.staging_namespace.name,
            secret_name="app-secrets-staging",
            aws_secret_name="app-secrets-staging",
        )

        # Add dependency to ensure namespace exists before creating external secret
        self.app_external_secret_staging.node.add_dependency(self.staging_namespace)

        ## TEST STUFF
        # TODO: remove all of this when we have deadicated environment clusters

        self.test_namespace = KubernetesNamespace(
            self,
            f"{self.config.environment}TestNamespace",
            eks_cluster=self.eks_cluster,
            name=f"{self.config.org_name}-{self.config.environment}-test",
        )

        self.app_secret_test = self.external_secrets.create_aws_secret(
            secret_name="app-secrets-test",
            secret_value={},
            description=f"Application secrets for {self.config.org_name} {self.config.environment} test environment",
        )

        self.app_external_secret_test = self.external_secrets.create_external_secret(
            target_namespace=self.test_namespace.name,
            secret_name="app-secrets-test",
            aws_secret_name="app-secrets-test",
        )

        # Add dependency to ensure namespace exists before creating external secret
        self.app_external_secret_test.node.add_dependency(self.test_namespace)

        ## OTHER K8S RESOURCES

        # Create EBS storage classes
        self.ebs_gp3_storage_class = StorageClass.create_ebs_gp3_storage_class(
            self,
            "EbsGp3StorageClass",
            eks_cluster=self.eks_cluster,
            name="ebs-gp3",
            is_default=True,
        )

        self.ebs_gp2_storage_class = StorageClass.create_ebs_gp2_storage_class(
            self,
            "EbsGp2StorageClass",
            eks_cluster=self.eks_cluster,
            name="ebs-gp2",
        )

        # Create ALB IngressClass
        # NOTE: No ACM certificate needed - Cloudflare provides SSL
        self.alb_ingress_class = IngressClass.create_alb_ingress_class(
            self,
            "AlbIngressClass",
            eks_cluster=self.eks_cluster,
            name="alb",
            group_name=f"{self.config.org_name}-{self.config.environment}-{self.config.aws_region}-alb",
        )
        # Ensure ALB IngressClass and IngressClassParams are created after the Load Balancer Controller is ready
        self.alb_ingress_class.node.add_dependency(self.load_balancer_controller.chart)
        self.alb_ingress_class.ingress_class_params.node.add_dependency(
            self.load_balancer_controller.chart
        )

    def _create_shared_secrets_sync(self) -> None:
        """Create ExternalSecret to sync shared secrets from AWS Secrets Manager to kube-system"""
        # ExternalSecret manifest for syncing shared-secrets to kube-system namespace
        # This creates individual K8s secrets from the shared AWS secret
        external_secret_manifest = {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {
                "name": "cloudflare-api-token-external",
                "namespace": "kube-system",
            },
            "spec": {
                "refreshInterval": "1h",
                "secretStoreRef": {
                    "name": f"{self.config.org_name}-{self.config.environment}-secrets",
                    "kind": "ClusterSecretStore",
                },
                "target": {
                    "name": "cloudflare-api-token",
                    "creationPolicy": "Owner",
                },
                "data": [
                    {
                        "secretKey": "apiToken",
                        "remoteRef": {
                            "key": self.shared_secrets_name,
                            "property": "CLOUDFLARE_API_TOKEN",
                        },
                    }
                ],
            },
        }

        self.cloudflare_external_secret = self.eks_cluster.cluster.add_manifest(
            "CloudflareExternalSecret",
            external_secret_manifest,
        )

        # Ensure the external secret is created after the Helm chart (for CRDs)
        self.cloudflare_external_secret.node.add_dependency(
            self.external_secrets.helm_chart
        )

        # Ensure the external secret is created after the cluster secret store
        if self.external_secrets.secret_store:
            self.cloudflare_external_secret.node.add_dependency(
                self.external_secrets.secret_store
            )

    @property
    def argocd_namespace(self):
        """Get the ArgoCD namespace"""
        return self.argocd.namespace

    def create_outputs(self, stack: cdk.Stack) -> None:
        """Create CloudFormation outputs for platform services"""
        # ArgoCD Outputs
        cdk.CfnOutput(
            stack,
            "ArgoCDNamespace",
            value=self.argocd_namespace.name,
            description="ArgoCD Namespace",
        )

        # Stack Namespace Outputs
        cdk.CfnOutput(
            stack,
            "EnvironmentNamespace",
            value=self.namespace.name,
            description="Environment Namespace",
        )
