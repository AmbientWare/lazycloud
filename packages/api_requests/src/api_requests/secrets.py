from models.secrets import SecretNoDeploymentId
from pydantic import BaseModel


class SecretsRequest(BaseModel):
    """Request to store secrets for a deployment."""

    secrets: list[SecretNoDeploymentId]
