from enum import StrEnum

from provider_cloudflare.workspace_storage import CloudflareWorkspaceStorageIssuer
from provider_garage.workspace_storage import GarageSettings, GarageWorkspaceStorageIssuer
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.deployment_settings import MissingDeploymentSettingError
from shared.workspace_storage import WorkspaceStorageIssuer
from storage_client.s3 import S3ObjectStoreSettings


class WorkspaceStorageProvider(StrEnum):
    Cloudflare = "cloudflare"
    Garage = "garage"


class WorkspaceStorageIssuerSettings(BaseSettings):
    issuer: WorkspaceStorageProvider | None = None

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_WORKSPACE_STORAGE_")


def managed_workspace_storage_issuer(settings: S3ObjectStoreSettings) -> WorkspaceStorageIssuer:
    match WorkspaceStorageIssuerSettings().issuer:
        case WorkspaceStorageProvider.Cloudflare:
            return CloudflareWorkspaceStorageIssuer(settings)
        case WorkspaceStorageProvider.Garage:
            return GarageWorkspaceStorageIssuer(settings, GarageSettings())
        case None:
            raise MissingDeploymentSettingError(
                "LAZYCLOUD_WORKSPACE_STORAGE_ISSUER", purpose="the workspace credential authority"
            )
