import aws_cdk as cdk
from infrastructure.config.environments import (
    PossibleEnvironments,
    get_environment_config,
    list_regional_environments,
)
from infrastructure.stacks import SharedStack
from infrastructure.stacks.prod import ProdInfraStack


def main() -> None:
    """Main application entry point

    2-Stack Architecture:
    1. shared - IAM, ECR, Secrets (cross-environment)
    2. infra  - VPC + EKS + Pod Identity + ConfigMap

    After infra deployment, install ArgoCD manually.
    """
    app = cdk.App()

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

    cdk.Tags.of(shared_stack).add("Environment", "shared")
    cdk.Tags.of(shared_stack).add("Organization", shared_config.org_name)
    cdk.Tags.of(shared_stack).add("ManagedBy", "AWS-CDK")

    for env_key in list_regional_environments():
        config = get_environment_config(env_key)
        stack_prefix = f"{config.org_name}-{config.environment}-{config.aws_region}"

        infra_stack = ProdInfraStack(
            app,
            f"{stack_prefix}-infra",
            config=config,
            shared_stack=shared_stack,
            description=f"Lazycloud - {config.environment.title()} - {config.aws_region}",
            env=cdk.Environment(
                account=config.aws_account_id,
                region=config.aws_region,
            ),
            cross_region_references=True,
        )

        cdk.Tags.of(infra_stack).add("Environment", config.environment)
        cdk.Tags.of(infra_stack).add("Region", config.aws_region)
        cdk.Tags.of(infra_stack).add("Organization", config.org_name)
        cdk.Tags.of(infra_stack).add("ManagedBy", "AWS-CDK")

    app.synth()


if __name__ == "__main__":
    main()
