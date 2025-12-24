import os
from dataclasses import dataclass
from enum import Enum

if not os.getenv("CDK_DEFAULT_ACCOUNT"):
    raise ValueError("CDK_DEFAULT_ACCOUNT is not set")


class PossibleEnvironments(Enum):
    PROD = "prod"


@dataclass
class EnvironmentConfig:
    """Configuration for a specific environment"""

    # Basic Environment Info
    environment: str
    aws_region: str
    org_name: str
    domain_name: str
    aws_account_id: str

    # Multi-Cluster Configuration
    cluster_id: str = ""
    is_hub: bool = False
    ecr_regions: list[str] | None = None

    # VPC Configuration
    vpc_cidr: str = "10.0.0.0/16"
    availability_zones: list[str] | None = None
    enable_dns_hostnames: bool = True
    enable_dns_support: bool = True
    single_nat_gateway: bool = False

    # Deployment Configuration
    is_regional: bool = (
        True  # If True, deploys regional stacks (infra, controllers, platform)
    )

    # App environments (namespaces) this cluster hosts
    # Each will get its own AWS secret: {org_name}/{env}-secrets
    app_environments: list[str] | None = None

    def __post_init__(self) -> None:
        """Set default values after initialization"""
        if self.availability_zones is None:
            if self.aws_region == "us-east-1":
                self.availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]
            elif self.aws_region == "us-west-2":
                self.availability_zones = ["us-west-2a", "us-west-2b", "us-west-2c"]
            else:
                # Default to first 3 AZs in the region
                self.availability_zones = [
                    f"{self.aws_region}a",
                    f"{self.aws_region}b",
                    f"{self.aws_region}c",
                ]


# Environment Configurations
ENVIRONMENTS: dict[str, EnvironmentConfig] = {
    "prod": EnvironmentConfig(
        aws_region="us-east-1",
        environment="prod",
        cluster_id="use1",
        is_hub=True,
        ecr_regions=["us-west-2"],
        vpc_cidr="10.0.0.0/16",
        single_nat_gateway=False,
        aws_account_id=os.getenv("CDK_DEFAULT_ACCOUNT"),
        org_name="lazycloud",
        domain_name="lazycloud.dev",
        app_environments=["prod", "staging"],
    ),
}


def get_environment_config(environment: str) -> EnvironmentConfig:
    """Get configuration for a specific environment"""
    if environment not in ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment: {environment}. Available: {list(ENVIRONMENTS.keys())}"
        )

    return ENVIRONMENTS[environment]


def list_environments() -> list[str]:
    """List all available environments"""
    return list(ENVIRONMENTS.keys())


def list_regional_environments() -> list[str]:
    """List all environments that should have regional stacks deployed"""
    return [
        env_key
        for env_key, env_config in ENVIRONMENTS.items()
        if env_config.is_regional
    ]
