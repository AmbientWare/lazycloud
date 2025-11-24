from pydantic import BaseModel

from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.secrets import SecretPydantic
from shared.models.compose import ComposeFile
from shared.models.helm import HelmValues


class ValidationResult(BaseModel):
    """Result from deployment validation task."""

    deployment: ComposeDeploymentPydantic
    compose_file: ComposeFile
    helm_values: HelmValues
    secrets: list[SecretPydantic]
