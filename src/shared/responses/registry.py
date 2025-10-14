from datetime import datetime

from pydantic import BaseModel


class UploadIntentResponse(BaseModel):
    """Response with ECR push credentials."""

    registry_url: str
    username: str
    password: str
    repository: str
    expires_at: datetime
