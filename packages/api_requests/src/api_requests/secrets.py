from models.secrets import BasicSecret
from pydantic import BaseModel


class SecretsRequest(BaseModel):
    """Request to store secrets for a deployment."""

    secrets: list[BasicSecret]
