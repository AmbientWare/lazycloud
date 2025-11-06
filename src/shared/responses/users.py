from pydantic import BaseModel


class OnboardingResponse(BaseModel):
    success: bool


class CurrentUserResponse(BaseModel):
    id: str


class WorkspaceFeatureResponse(BaseModel):
    limit: int
    deployment_limit: int
    current_count: int


class DeploymentFeatureResponse(BaseModel):
    service_limit: int
    volume_limit: int
    network_limit: int


class UserFeaturesResponse(BaseModel):
    workspace: WorkspaceFeatureResponse
    deployment: DeploymentFeatureResponse
    domain_limit: int
