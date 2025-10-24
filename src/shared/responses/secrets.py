from pydantic import BaseModel

from shared.models.secrets import Secret


class SecretsResponse(BaseModel):
    """Response for secrets"""

    secrets: list[Secret]


class SecretsStoredResponse(BaseModel):
    """Response confirming secrets were stored."""

    deployment_id: str
    secrets_count: int
    message: str = "Secrets stored successfully"
