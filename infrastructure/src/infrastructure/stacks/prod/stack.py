import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.components import (
    SharedInfrastructure,
)
from infrastructure.stacks.shared import SharedStack


class ProdStack(cdk.Stack):
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

        # Create shared infrastructure (VPC, EKS)
        self.shared_infrastructure = SharedInfrastructure(
            self,
            "SharedInfrastructure",
            config=self.config,
            shared_stack=self.shared_stack,
        )

        # Create shared storage (S3)
        # self.shared_storage = SharedStorage(
        #     self,
        #     "SharedStorage",
        #     config=self.config,
        #     vpc=self.shared_infrastructure.vpc,
        # )

        # Create platform services (ArgoCD, Helm charts)
        # self.platform_services = PlatformServices(
        #     self,
        #     "PlatformServices",
        #     config=self.config,
        #     eks_cluster=self.shared_infrastructure.eks_cluster,
        #     external_secrets_role_arn=self.shared_stack.iam_roles.external_secrets_role_arn,
        #     load_balancer_controller=self.shared_infrastructure.load_balancer_controller,
        #     certificate_arn=self.shared_stack.dns.certificates[
        #         "wildcard"
        #     ].certificate_arn,
        # )

        # # Create environment-specific applications
        # self.environment_apps = EnvironmentApps(
        #     self,
        #     "EnvironmentApps",
        #     config=self.config,
        #     eks_cluster=self.shared_infrastructure.eks_cluster,
        #     apps=get_apps(self.platform_services.namespace.name),
        # )

        # Create all outputs
        self._create_outputs()

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs"""
        # Shared infrastructure outputs
        self.shared_infrastructure.create_outputs(self)

        # Shared storage outputs
        # self.shared_storage.create_outputs(self)

        # Platform services outputs
        # self.platform_services.create_outputs(self)
