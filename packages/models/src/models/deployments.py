from enum import StrEnum

from models.helm import HelmValues
from pydantic import BaseModel


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


class DeploymentResult(BaseModel):
    """Result from application deployment task."""

    revision: int | None = None
