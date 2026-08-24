from __future__ import annotations

from dataclasses import dataclass

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

    No default, because the two stores are reached in different ways and a wrong
    guess fails inside a worker rather than at startup. `MissingDeploymentSettingError`
    names the variable, which is the only thing an operator can act on.
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

    Both are constructed the same way from the caller's point of view, which is
    what keeps a second store from becoming a second code path anywhere above
    this line.
    """

    state: WorkspaceStorageStateStore
    settings: WorkspaceStorageIssuerSettings

    def create(self) -> WorkspaceStorageIssuer:
        kind = self.settings.kind()
        if kind is WorkspaceStorageIssuerKind.Garage:
            garage = GarageAdminSettings()
            if not garage.endpoint_url or not garage.token:
                raise MissingDeploymentSettingError(
                    f"{ENV_PREFIX}_GARAGE_ADMIN_ENDPOINT_URL",
                    purpose="the Garage admin API this deployment mints workspace keys through",
                )
            return GarageWorkspaceStorageIssuer(
                admin=GarageAdminClient(
                    endpoint_url=garage.endpoint_url,
                    token=garage.token,
                ),
                state=self.state,
                lifetime_seconds=garage.credential_lifetime_seconds,
            )
        msg = f"{kind.value} workspace storage issuer is not available in this build"
        raise MissingDeploymentSettingError(_ISSUER_VARIABLE, purpose=msg)


__all__ = [
    "WorkspaceStorageIssuerFactory",
    "WorkspaceStorageIssuerSettings",
]
