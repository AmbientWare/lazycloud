from __future__ import annotations

from dataclasses import dataclass

from database.client import DatabaseClient
from database.repositories.aws_connections import AwsAccountConnectionRepository
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.aws_connections import AwsAccountConnection
from shared.errors import UpstreamUnavailableError
from shared.identity import WorkspaceRecord
from shared.workspace_storage import (
    ConnectedWorkspaceStorageIssuer,
    WorkspaceStorageGrant,
    WorkspaceStorageIssuer,
    WorkspaceStorageProvider,
)


class WorkspaceStorageIssuerSettings(BaseSettings):
    issuer: WorkspaceStorageProvider | None = None

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_WORKSPACE_STORAGE_")


@dataclass(frozen=True, slots=True)
class WorkspaceStorageRouter:
    """Send each workspace's storage calls to the authority for where it lives.

    A workspace without a connection is in platform storage; one with a connection
    keeps its bucket in that connected account and is credentialed through the
    connection's role.
    """

    database: DatabaseClient
    managed: WorkspaceStorageIssuer
    connected: ConnectedWorkspaceStorageIssuer | None = None

    def issue(self, workspace: WorkspaceRecord) -> WorkspaceStorageGrant:
        if workspace.connection_id is None:
            return self.managed.issue(workspace)
        return self._connected().issue(workspace, self._connection(workspace))

    def retire(self, workspace: WorkspaceRecord) -> None:
        if not workspace.storage.bucket:
            return
        if workspace.connection_id is None:
            self.managed.retire(workspace)
            return
        self._connected().retire(workspace, self._connection(workspace))

    def _connected(self) -> ConnectedWorkspaceStorageIssuer:
        if self.connected is None:
            raise UpstreamUnavailableError("connected cloud storage is not configured")
        return self.connected

    def _connection(self, workspace: WorkspaceRecord) -> AwsAccountConnection:
        if workspace.connection_id is None:
            raise UpstreamUnavailableError("workspace does not live in a connected account")
        with self.database.session() as session:
            connection = AwsAccountConnectionRepository(session).get(workspace.connection_id)
        if connection is None:
            raise UpstreamUnavailableError(
                f"connected account for workspace {workspace.id!r} no longer exists"
            )
        return connection


__all__ = [
    "WorkspaceStorageIssuerSettings",
    "WorkspaceStorageRouter",
]
