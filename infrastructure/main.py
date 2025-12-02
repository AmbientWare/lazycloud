import aws_cdk as cdk
from infrastructure.config.environments import (
    PossibleEnvironments,
    get_environment_config,
    list_regional_environments,
)
from infrastructure.stacks import SharedStack
from infrastructure.stacks.prod import (
    ProdControllersStack,
    ProdInfraStack,
    ProdPlatformStack,
)


def main() -> None:
    """Main application entry point

    4-Stack Architecture (Industry Standard):
    1. shared         - IAM, ECR, Secrets (cross-environment)
    2. prod-infra     - VPC + EKS cluster (stable foundation)
    3. prod-controllers - Karpenter, ALB Controller, EBS CSI (AWS controllers)
    4. prod-platform  - ArgoCD, monitoring, apps (frequently updated)

    Deployment order: shared → infra → controllers → platform

    NOTE: Regional stacks are automatically deployed for all environments
          with is_regional=True in environments.py
    """
    app = cdk.App()

    # Create shared infrastructure stack (deploy once, not per environment)
    # Use prod config for shared stack (could use any config since we override zone_name)
    shared_config = get_environment_config(PossibleEnvironments.PROD.value)
    shared_stack = SharedStack(
        app,
        f"{shared_config.org_name}-shared",
        config=shared_config,
        env=cdk.Environment(
            account=shared_config.aws_account_id,
            region=shared_config.aws_region,
        ),
        cross_region_references=True,
    )

    # Add tags to shared stack
    cdk.Tags.of(shared_stack).add("Environment", "shared")
    cdk.Tags.of(shared_stack).add("Organization", shared_config.org_name)
    cdk.Tags.of(shared_stack).add("ManagedBy", "AWS-CDK")
    cdk.Tags.of(shared_stack).add("Project", "lazycloud-infrastructure")

    # Automatically deploy regional stacks for all environments with is_regional=True
    for env_key in list_regional_environments():
        config = get_environment_config(env_key)

        # Create stack name prefix with region for multi-region support
        stack_prefix = f"{config.org_name}-{config.environment}-{config.aws_region}"

        # Stack 1: Infrastructure (VPC + EKS)
        infra_stack = ProdInfraStack(
            app,
            f"{stack_prefix}-infra",
            config=config,
            shared_stack=shared_stack,
            description=f"Lazycloud Infrastructure - {config.environment.title()} - {config.aws_region} - Core (VPC + EKS)",
            env=cdk.Environment(
                account=config.aws_account_id,
                region=config.aws_region,
            ),
            cross_region_references=True,
        )

        # Stack 2: Controllers (Karpenter, ALB Controller)
        controllers_stack = ProdControllersStack(
            app,
            f"{stack_prefix}-controllers",
            config=config,
            shared_stack=shared_stack,
            infra_stack=infra_stack,
            description=f"Lazycloud Infrastructure - {config.environment.title()} - {config.aws_region} - Controllers (Karpenter, ALB)",
            env=cdk.Environment(
                account=config.aws_account_id,
                region=config.aws_region,
            ),
            cross_region_references=True,
        )

        # Stack 3: Platform (ArgoCD, monitoring, apps)
        platform_stack = ProdPlatformStack(
            app,
            f"{stack_prefix}-platform",
            config=config,
            shared_stack=shared_stack,
            infra_stack=infra_stack,
            controllers_stack=controllers_stack,
            description=f"Lazycloud Infrastructure - {config.environment.title()} - {config.aws_region} - Platform (ArgoCD, Apps)",
            env=cdk.Environment(
                account=config.aws_account_id,
                region=config.aws_region,
            ),
            cross_region_references=True,
        )

        # Add environment-specific tags to all stacks
        for stack in [infra_stack, controllers_stack, platform_stack]:
            cdk.Tags.of(stack).add("Environment", config.environment)
            cdk.Tags.of(stack).add("Region", config.aws_region)
            cdk.Tags.of(stack).add("Organization", config.org_name)
            cdk.Tags.of(stack).add("ManagedBy", "AWS-CDK")
            cdk.Tags.of(stack).add("Project", "lazycloud-infrastructure")

    app.synth()


if __name__ == "__main__":
    main()
