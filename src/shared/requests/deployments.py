from enum import StrEnum

from pydantic import BaseModel, Field


class DeploymentCreateRequest(BaseModel):
    compose_yaml: str = Field(..., description="Docker Compose YAML content")
    workspace_id: str = Field(..., description="Workspace ID")
    name: str | None = Field(
        None,
        description="Unique deployment name for updates",
        pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
    )
    secrets: bool = Field(False, description="Whether to wait for secrets to be stored")


class DiffType(StrEnum):
    NEW = "new"
    EXISTING = "existing"


class DiffRequest(BaseModel):
    diff_type: DiffType = Field(..., description="Type of diff to perform")
    workspace_id: str = Field(..., description="Workspace ID")
    deployment_name: str = Field(..., description="Deployment name")
    compose_yaml: str = Field(..., description="New Docker Compose YAML content")
    env_keys: list[str] = Field(
        default_factory=list, description="List of environment variable keys"
    )


class RollbackRequest(BaseModel):
    revision: int = Field(..., description="Helm revision number to rollback to")
