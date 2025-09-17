from datetime import datetime

from pydantic import BaseModel

from shared.models.deployments import DeploymentStates
from shared.models.diffs import ComposeDiff, EnvVarChanges
from shared.models.statuses import DeploymentStatus


class DeploymentResponse(BaseModel):
    """Response for a deployment."""

    id: str
    user_id: str
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
    skip: int
    limit: int


class DiffResponse(BaseModel):
    """Response for a deployment diff operation."""

    deployment_id: str
    namespace: str
    has_changes: bool
    diff: ComposeDiff
    env_var_changes: EnvVarChanges | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None


class RestartServiceResult(BaseModel):
    """Result of a single service restart operation."""

    service: str
    success: bool
    message: str
    resource_type: str
    resource_name: str
    output: str | None = None
    error: str | None = None


class RestartResponse(BaseModel):
    """Response for restart operations."""

    deployment_id: str
    deployment_name: str
    service_name: str | None = None  # None for all services
    success: bool
    message: str
    resource_type: str | None = None  # For single service
    resource_name: str | None = None  # For single service
    output: str | None = None  # For single service
    total_services: int | None = None  # For all services
    successful: int | None = None  # For all services
    failed: int | None = None  # For all services
    results: list[RestartServiceResult] | None = (
        None  # Detailed results for all services
    )


class ValidationResult(BaseModel):
    """Result of a deployment validation operation."""

    errors: list[str]
    warnings: list[str]
    can_deploy: bool


class ServiceResourceStatus(BaseModel):
    """Status of a service resource."""

    type: str  # container, volume, network, config
    name: str
    status: str  # running, stopped, pending, error
    message: str | None = None


class DeploymentStatusResponse(BaseModel):
    """Response for deployment status."""

    status: DeploymentStatus
