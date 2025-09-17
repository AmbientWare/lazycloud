from pydantic import BaseModel


class SecretsStoredResponse(BaseModel):
    """Response confirming secrets were stored."""

    deployment_id: str
    secrets_count: int
    message: str = "Secrets stored successfully"
