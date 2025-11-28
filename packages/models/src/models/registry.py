from datetime import datetime

from pydantic import BaseModel


class ECRCredentials(BaseModel):
    """ECR push credentials for a tenant."""

    registry_url: str
    username: str
    password: str
    repository: str
    expires_at: datetime
