"""Schemas for task data transfer."""

from models.compose import ComposeFile
from models.helm import HelmValues
from pydantic import BaseModel

from backend.database.compose import ComposeDeploymentPydantic
from backend.database.secrets import SecretPydantic


class DeploymentPreparationResult(BaseModel):
    """Result from deployment preparation containing parsed and processed deployment data."""

    deployment: ComposeDeploymentPydantic
    compose_file: ComposeFile | None
    helm_values: HelmValues
    secrets: list[SecretPydantic]
