from datetime import datetime

from pydantic import BaseModel

from shared.models.deployments import DeploymentStates
from shared.models.diffs import ComposeDiff, EnvVarChanges
from shared.models.statuses import DeploymentStatus


class DeploymentResponse(BaseModel):
    """Response for a deployment."""

    id: str
    workspace_id: str
    name: str
    namespace: str
    state: DeploymentStates
    status_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deployed_at: datetime | None = None


class DeploymentListResponse(BaseModel):
    """List of deployments."""

    deployments: list[DeploymentResponse]
    total: int
    limit: int
    has_more: bool
    cursor: str | None = None


class DiffResponse(BaseModel):
    """Response for a deployment diff operation."""

    deployment_id: str
    namespace: str
    has_changes: bool
    diff: ComposeDiff
    env_var_changes: EnvVarChanges | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None


class ValidationResult(BaseModel):
    """Result of a deployment validation operation."""

    errors: list[str]
    warnings: list[str]
    can_deploy: bool


class DeploymentStatusResponse(BaseModel):
    """Response for deployment status."""

    status: DeploymentStatus


class DeploymentOverview(BaseModel):
    """Overview of a deployment with basic info and resource counts."""

    id: str
    workspace_id: str
    name: str
    namespace: str
    state: DeploymentStates
    status_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deployed_at: datetime | None = None
    service_count: int
    volume_count: int
    ready_services: int | None = None
