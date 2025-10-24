from pydantic import BaseModel

from shared.models.secrets import Secret


class SecretsRequest(BaseModel):
    """Request to store secrets for a deployment."""

    secrets: list[Secret]
