from models.secrets import BasicSecret
from pydantic import BaseModel


class SecretsResponse(BaseModel):
    """Response for secrets"""

    secrets: list[BasicSecret]


class SecretsStoredResponse(BaseModel):
    """Response confirming secrets were stored."""

    deployment_id: str
    secrets_count: int
    message: str = "Secrets stored successfully"
