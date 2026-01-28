from pydantic import BaseModel


class OnboardingResponse(BaseModel):
    success: bool


class CurrentUserResponse(BaseModel):
    id: str
    workos_id: str


class UserFeaturesResponse(BaseModel):
    """User subscription features response.

    Workspaces are unlimited (organizational only).
    Services, volumes, and networks per deployment are unlimited.
    """

    deployment_limit: int
    deployment_count: int  # Current total deployments across all workspaces
    max_team_members: int | None  # None = unlimited
    max_cpu_per_service: float
    max_memory_per_service: int  # GB
    max_replicas_per_service: int
    custom_domains_enabled: bool
