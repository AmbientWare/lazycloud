"""Schemas for task data transfer."""

from models.compose import ComposeFile
from models.helm import HelmValues
from pydantic import BaseModel

from backend.database.models import ComposeDeployment, Secret


class DeploymentPreparationResult(BaseModel):
    """Result from deployment preparation containing parsed and processed deployment data."""

    deployment: ComposeDeployment
    compose_file: ComposeFile | None
    helm_values: HelmValues
    secrets: list[Secret]
