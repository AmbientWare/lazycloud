import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import EksCluster
from infrastructure.constructs.components.controllers import ControllersConstruct
from infrastructure.stacks.shared import SharedStack
from infrastructure.stacks.prod.infra_stack import ProdInfraStack


class ProdControllersStack(cdk.Stack):
    """Production controllers stack: Karpenter, ALB Controller, EBS CSI

    This stack contains AWS-specific controllers that:
    - Manage cluster autoscaling (Karpenter)
    - Manage load balancers (AWS Load Balancer Controller)
    - Are updated monthly/quarterly
    - Should not cause EKS cluster rollback if they fail

    Update frequency: Monthly (for controller version upgrades)
    Deployment time: ~5-10 minutes
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        shared_stack: SharedStack,
        infra_stack: ProdInfraStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.shared_stack = shared_stack
        self.infra_stack = infra_stack

        # Import values from infra stack
        # Note: We use the EKS cluster object directly from infra_stack
        # to avoid circular dependencies
        self.eks_cluster = infra_stack.infrastructure.eks_cluster
        self.vpc = infra_stack.infrastructure.vpc
        self.vpc_id = infra_stack.infrastructure.vpc_id

        # Create controllers
        self.controllers = ControllersConstruct(
            self,
            "Controllers",
            config=self.config,
            shared_stack=self.shared_stack,
            eks_cluster=self.eks_cluster,
            vpc_id=self.vpc_id,
            vpc=self.vpc,
        )

        # Add dependency on infra stack
        self.add_dependency(infra_stack)
