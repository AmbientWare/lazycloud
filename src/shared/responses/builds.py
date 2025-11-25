from datetime import datetime

from pydantic import BaseModel, Field


class DepotTokenResponse(BaseModel):
    """Response with Depot project token for CLI builds."""

    project_id: str = Field(description="Depot project ID for this workspace")
    token: str = Field(description="Short-lived project token for depot build")
    expires_at: datetime = Field(description="Token expiration time")
    registry_url: str = Field(description="ECR registry URL to push images to")
