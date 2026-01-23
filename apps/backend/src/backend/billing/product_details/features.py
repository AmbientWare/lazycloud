from pydantic import BaseModel, ConfigDict, Field


class BaseFeatures(BaseModel):
    """Subscription feature limits.

    Simplified flat structure with 6 core limits:
    - deployment_limit: Total deployments across all workspaces
    - max_team_members: Team members per workspace
    - max_cpu_per_service: CPU cores per service
    - max_memory_per_service: Memory GB per service
    - max_replicas_per_service: Replicas for auto-scaling
    - custom_domains_enabled: Whether custom domains are allowed

    Workspaces are unlimited (organizational only).
    Services, volumes, and networks per deployment are unlimited (usage billing handles cost).
    """

    deployment_limit: int = Field(
        description="Maximum total deployments across all workspaces"
    )
    max_team_members: int | None = Field(
        default=1,
        description="Maximum team members per workspace (None = unlimited)",
    )
    max_cpu_per_service: float = Field(
        default=1.0,
        description="Maximum CPU cores per service",
    )
    max_memory_per_service: int = Field(
        default=2,
        description="Maximum memory GB per service",
    )
    max_replicas_per_service: int = Field(
        default=1,
        description="Maximum replicas allowed per service (for auto-scaling)",
    )
    custom_domains_enabled: bool = Field(
        default=False,
        description="Whether custom domains are allowed",
    )
    support_level: str = Field(
        default="community",
        description="Support tier: community, email, priority, dedicated",
    )

    model_config = ConfigDict(extra="ignore")
