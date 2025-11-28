from models.secrets import Secret
from pydantic import BaseModel


class SecretsRequest(BaseModel):
    """Request to store secrets for a deployment."""

    secrets: list[Secret]
