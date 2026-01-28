from datetime import datetime

from models.deployments import DeploymentStates
from models.diffs import ComposeDiff, EnvVarChanges, StorageTypeChange
from models.statuses import DeploymentStatus
from pydantic import BaseModel


class ServiceEndpoints(BaseModel):
    """Endpoints for a service."""

    internal: str  # Kubernetes internal DNS, e.g., http://api:8000
    public: str | None = None  # External URL, e.g., https://api-xxxxx.lazycloud.dev


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
    cluster_id: str | None = None  # Cluster where deployment is running
    endpoints: dict[str, ServiceEndpoints] | None = None  # Per-service endpoints


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
    storage_type_changes: list[StorageTypeChange] | None = None
    errors: list[str] | None = None
    warnings: list[str] | None = None
    can_deploy: bool = True
    existing_compose_yaml: str | None = None
    endpoints: dict[str, ServiceEndpoints] | None = None  # Per-service endpoints


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
    service_count: int
    volume_count: int
    network_count: int
    status_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deployed_at: datetime | None = None
    cluster_id: str | None = None
    ready_services: int | None = None


class Revision(BaseModel):
    """Revision information."""

    revision: int
    status: str
    chart: str
    description: str
    updated: str


class DeploymentHistoryResponse(BaseModel):
    """Response for deployment history."""

    revisions: list[Revision]
