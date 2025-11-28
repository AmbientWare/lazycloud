from models.compose import ComposeFile
from models.helm import HelmValues
from pydantic import BaseModel

from backend.database.compose import ComposeDeploymentPydantic
from backend.database.secrets import SecretPydantic


class ValidationResult(BaseModel):
    """Result from deployment validation task."""

    deployment: ComposeDeploymentPydantic
    compose_file: ComposeFile
    helm_values: HelmValues
    secrets: list[SecretPydantic]
