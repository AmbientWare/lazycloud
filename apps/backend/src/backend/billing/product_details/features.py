from pydantic import BaseModel, ConfigDict, Field


class WorkspaceFeature(BaseModel):
    """Workspace-related feature limits."""

    limit: int = Field(description="Maximum number of workspaces")
    deployment_limit: int = Field(description="Maximum deployments per workspace")


class DeploymentFeature(BaseModel):
    """Deployment-related feature limits."""

    service_limit: int = Field(description="Maximum services per deployment")
    volume_limit: int = Field(description="Maximum volumes per deployment")
    network_limit: int = Field(description="Maximum networks per deployment")
    max_replicas_per_service: int = Field(
        default=10,
        description="Maximum replicas allowed per service (for scaling limits)",
    )
    max_cpu_per_service: float | None = Field(
        default=None,
        description="Maximum CPU cores per service (None = unlimited)",
    )
    max_memory_per_service: int | None = Field(
        default=None,
        description="Maximum memory GB per service (None = unlimited)",
    )


class BaseFeatures(BaseModel):
    """Subscription feature limits.

    When adding new fields in the future:
    1. Make them optional with defaults for backwards compatibility
    2. Example: new_feature: int | None = Field(default=None, description="...")
    3. Old metadata without the field will use the default value
    """

    workspace: WorkspaceFeature
    deployment: DeploymentFeature
    domain_limit: int = Field(
        description="Maximum custom domains (0 = platform domains only)"
    )
    max_team_members: int | None = Field(
        default=1,
        description="Maximum team members per workspace (None = unlimited)",
    )
    support_level: str = Field(
        default="community",
        description="Support tier: community, email, priority",
    )

    model_config = ConfigDict(extra="ignore")
