import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.components.platform_services import PlatformServices
from infrastructure.stacks.shared import SharedStack
from infrastructure.stacks.prod.infra_stack import ProdInfraStack
from infrastructure.stacks.prod.controllers_stack import ProdControllersStack


class ProdPlatformStack(cdk.Stack):
    """Production platform stack: ArgoCD, monitoring, observability, apps

    This stack contains platform services that:
    - Are deployed on top of the EKS cluster
    - Change frequently (weekly/daily)
    - Include ArgoCD, Prometheus, Loki, External Secrets
    - Include application namespaces and workloads

    Update frequency: Weekly/Daily (for app deployments)
    Deployment time: ~10-15 minutes
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
        infra_stack: ProdInfraStack,
        controllers_stack: ProdControllersStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.shared_stack = shared_stack
        self.infra_stack = infra_stack
        self.controllers_stack = controllers_stack

        # Import values from other stacks
        self.eks_cluster = infra_stack.infrastructure.eks_cluster
        self.load_balancer_controller = controllers_stack.controllers.load_balancer_controller

        # Get shared secrets name if it exists
        shared_secrets_name = (
            shared_stack.shared_secrets.secret_name
            if hasattr(shared_stack, "shared_secrets")
            else None
        )

        # Create platform services (ArgoCD, monitoring, etc.)
        # NOTE: These are currently commented out in the original stack
        # Uncomment when ready to deploy
        # self.platform_services = PlatformServices(
        #     self,
        #     "PlatformServices",
        #     config=self.config,
        #     eks_cluster=self.eks_cluster,
        #     external_secrets_role_arn=self.shared_stack.iam_roles.external_secrets_role_arn,
        #     load_balancer_controller=self.load_balancer_controller,
        #     shared_secrets_name=shared_secrets_name,
        # )

        # Add dependencies
        self.add_dependency(infra_stack)
        self.add_dependency(controllers_stack)

        # Create outputs
        self._create_outputs()

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs for platform stack"""

        cdk.CfnOutput(
            self,
            "PlatformStackReady",
            value="true",
            description="Platform stack deployment complete",
        )

        # When platform services are enabled, add outputs for ArgoCD, Grafana, etc.
        # Example:
        # cdk.CfnOutput(
        #     self,
        #     "ArgoCDUrl",
        #     value=f"https://argocd.{self.config.domain_name}",
        #     description="ArgoCD URL",
        # )
