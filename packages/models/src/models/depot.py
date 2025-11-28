from datetime import datetime

from pydantic import BaseModel, Field


class DepotProject(BaseModel):
    """Depot project information."""

    id: str = Field(description="Depot project ID")
    name: str = Field(description="Project name")


class DepotProjectToken(BaseModel):
    """Token for authenticating Depot CLI builds."""

    token: str = Field(description="Project token value")
    expires_at: datetime = Field(description="Token expiration time")


class DepotBuildCredentials(BaseModel):
    """Credentials for running a Depot build."""

    project_id: str = Field(description="Depot project ID")
    token: str = Field(description="Project token for authentication")
    expires_at: datetime = Field(description="Token expiration time")
    registry_url: str = Field(description="ECR registry URL to push images to")
