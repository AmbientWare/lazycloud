from pydantic import BaseModel


class SecretCollection(BaseModel):
    added: dict[str, str] | None = None
    removed: list[str] | None = None


class SecretsRequest(BaseModel):
    """Request to store secrets for a deployment."""

    # Simple key-value pairs for all secrets
    secrets_collection: SecretCollection
