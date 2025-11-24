from enum import StrEnum

from pydantic import BaseModel

from shared.models.helm import HelmValues


class DeploymentStates(StrEnum):
    """Status of a deployment."""

    PENDING = "pending"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class DeploymentInfo(BaseModel):
    """Deployment information returned from idempotency check."""

    id: str
    workspace_id: str
    name: str
    namespace: str
    current_helm_values: HelmValues | None = None


class ResourceRequirements(BaseModel):
    """Resource requirements for a deployment."""

    deployments: int
    services: int
    pvcs: int


class DeploymentResult(BaseModel):
    """Result from application deployment task."""

    revision: int | None = None
