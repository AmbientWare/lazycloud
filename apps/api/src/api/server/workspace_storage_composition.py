from __future__ import annotations

from dataclasses import dataclass

from provider_clients.workspace_storage import aws_workspace_storage_issuer
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError
from shared.workspace_storage import WorkspaceStorageIssuer, WorkspaceStorageIssuerKind
from storage_client.garage import (
    GarageAdminClient,
    GarageAdminSettings,
    GarageWorkspaceStorageIssuer,
    WorkspaceStorageStateStore,
)

_ISSUER_VARIABLE = f"{ENV_PREFIX}_WORKSPACE_STORAGE_ISSUER"


class WorkspaceStorageIssuerSettings(BaseSettings):
    """Which store grants a workspace access to its own bucket.

    No default. The two stores are reached in different ways, and a wrong guess
    surfaces as a mount failure inside a worker rather than as a missing setting
    at startup.
    """

    issuer: WorkspaceStorageIssuerKind | None = None

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_WORKSPACE_STORAGE_",
        extra="ignore",
    )

    def kind(self) -> WorkspaceStorageIssuerKind:
        if self.issuer is None:
            raise MissingDeploymentSettingError(
                _ISSUER_VARIABLE,
                purpose="which object store grants a workspace access to its own bucket",
            )
        return self.issuer


@dataclass(frozen=True, slots=True)
class WorkspaceStorageIssuerFactory:
    """Builds the one issuer this deployment uses.

    Lives in composition because it names both a provider adapter and the local
    store's client, and nothing below this line may depend on both.
    """

    state: WorkspaceStorageStateStore
    settings: WorkspaceStorageIssuerSettings

    def create(self) -> WorkspaceStorageIssuer:
        kind = self.settings.kind()
        if kind is WorkspaceStorageIssuerKind.Aws:
            return aws_workspace_storage_issuer()
        garage = GarageAdminSettings()
        if not garage.endpoint_url or not garage.token:
            raise MissingDeploymentSettingError(
                f"{ENV_PREFIX}_GARAGE_ADMIN_ENDPOINT_URL",
                purpose="the Garage admin API this deployment mints workspace keys through",
            )
        return GarageWorkspaceStorageIssuer(
            admin=GarageAdminClient(endpoint_url=garage.endpoint_url, token=garage.token),
            state=self.state,
            lifetime_seconds=garage.credential_lifetime_seconds,
        )


__all__ = [
    "WorkspaceStorageIssuerFactory",
    "WorkspaceStorageIssuerSettings",
]
