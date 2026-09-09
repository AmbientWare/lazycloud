from provider_aws.storage_access import AwsStorageAccessSettings
from provider_aws.workspace_storage import AwsWorkspaceStorageIssuer, AwsWorkspaceStorageSettings
from provider_garage.workspace_storage import GarageSettings, GarageWorkspaceStorageIssuer
from shared.deployment_settings import MissingDeploymentSettingError
from shared.workspace_storage import WorkspaceStorageIssuer, WorkspaceStorageProvider
from storage.workspace_storage_issuers import WorkspaceStorageIssuerSettings
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
