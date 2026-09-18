from __future__ import annotations

from database.client import DatabaseClient
from provider_aws.connected_workspace_storage import AwsConnectedWorkspaceStorage
from provider_aws.storage_access import AwsStorageAccessSettings
from provider_aws.workspace_storage import AwsWorkspaceStorageIssuer, AwsWorkspaceStorageSettings
from provider_garage.workspace_storage import GarageSettings, GarageWorkspaceStorageIssuer
from shared.deployment_settings import MissingDeploymentSettingError
from shared.workspace_storage import WorkspaceStorageIssuer, WorkspaceStorageProvider
from storage.workspace_storage_issuers import WorkspaceStorageIssuerSettings, WorkspaceStorageRouter
from storage_client.s3 import S3ObjectStoreSettings


def managed_workspace_storage_issuer(settings: S3ObjectStoreSettings) -> WorkspaceStorageIssuer:
    match WorkspaceStorageIssuerSettings().issuer:
        case WorkspaceStorageProvider.Aws:
            return AwsWorkspaceStorageIssuer(
                settings, AwsWorkspaceStorageSettings(), AwsStorageAccessSettings()
            )
        case WorkspaceStorageProvider.Garage:
            return GarageWorkspaceStorageIssuer(settings, GarageSettings())
        case None:
            raise MissingDeploymentSettingError(
                "LAZYCLOUD_WORKSPACE_STORAGE_ISSUER", purpose="the workspace credential authority"
            )


def connected_workspace_storage(*, public_origin: str) -> AwsConnectedWorkspaceStorage:
    return AwsConnectedWorkspaceStorage.from_default_chain(public_origin=public_origin)


def workspace_storage_router(
    database: DatabaseClient,
    settings: S3ObjectStoreSettings,
    *,
    public_origin: str,
) -> WorkspaceStorageRouter:
    return WorkspaceStorageRouter(
        database=database,
        managed=managed_workspace_storage_issuer(settings),
        connected=connected_workspace_storage(public_origin=public_origin),
    )


__all__ = [
    "connected_workspace_storage",
    "managed_workspace_storage_issuer",
    "workspace_storage_router",
]
