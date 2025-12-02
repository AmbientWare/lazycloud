import aws_cdk as cdk
from infrastructure.stacks import ProdStack, SharedStack
from infrastructure.config.environments import (
    get_environment_config,
    PossibleEnvironments,
)


def main() -> None:
    """Main application entry point"""
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
    )

    # Add tags to shared stack
    cdk.Tags.of(shared_stack).add("Environment", "shared")
    cdk.Tags.of(shared_stack).add("Organization", shared_config.org_name)
    cdk.Tags.of(shared_stack).add("ManagedBy", "AWS-CDK")
    cdk.Tags.of(shared_stack).add("Project", "lazycloud-infrastructure")

    # Create stacks for specified environments
    for env_name in PossibleEnvironments:
        config = get_environment_config(env_name.value)

        if env_name == PossibleEnvironments.PROD:
            stack = ProdStack(
                app,
                f"{config.org_name}-{env_name.value}",
                config=config,
                shared_stack=shared_stack,
                description=f"Lazycloud Infrastructure - {config.environment.title()} Environment",
                env=cdk.Environment(
                    account=config.aws_account_id,
                    region=config.aws_region,
                ),
            )

        else:
            raise ValueError(f"Invalid environment: {env_name}")

        # Add environment-specific tags
        cdk.Tags.of(stack).add("Environment", config.environment)
        cdk.Tags.of(stack).add("Organization", config.org_name)
        cdk.Tags.of(stack).add("ManagedBy", "AWS-CDK")
        cdk.Tags.of(stack).add("Project", "lazycloud-infrastructure")

    app.synth()


if __name__ == "__main__":
    main()
