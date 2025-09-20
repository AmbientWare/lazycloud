from pydantic import BaseModel

from shared.models.secrets import SecretCollection


class SecretsRequest(BaseModel):
    """Request to store secrets for a deployment."""

    secrets_collection: SecretCollection
