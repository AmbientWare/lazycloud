from pydantic import BaseModel


class WorkspaceFeature(BaseModel):
    limit: int
    deployment_limit: int


class DeploymentFeature(BaseModel):
    service_limit: int
    volume_limit: int
    network_limit: int


class BaseFeatures(BaseModel):
    workspace: WorkspaceFeature
    deployment: DeploymentFeature
    domain_limit: int
