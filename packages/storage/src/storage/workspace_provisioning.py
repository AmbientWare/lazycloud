from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from database.repositories.aws_connections import AwsAccountConnectionRepository
from shared.aws_connections import AwsAccountConnection
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.identity import WorkspaceRecord, WorkspaceStorageConfig
from shared.workspace_storage import ConnectedWorkspaceStorageIssuer

from database import DatabaseClient


class WorkspaceStorageError(UpstreamUnavailableError):
    pass


class WorkspaceStorageAlreadyExistsError(ConflictError):
    pass


class WorkspaceBucketSettings(Protocol):
    @property
    def workspace_bucket_prefix(self) -> str: ...

    @property
    def endpoint_url(self) -> str | None: ...

    @property
    def region_name(self) -> str: ...


@runtime_checkable
class WorkspaceBucketClient(Protocol):
    @property
    def settings(self) -> WorkspaceBucketSettings: ...

    def create_bucket(self, bucket: str | None = None) -> None: ...

    def validate_bucket_access(self, bucket: str | None = None) -> None: ...

    def configure_workspace_bucket(self, bucket: str, *, public_origin: str) -> None: ...


@dataclass(slots=True)
class WorkspaceBucketProvisioner:
    database: DatabaseClient
    client: WorkspaceBucketClient | None = None
    public_origin: str = ""
    connected: ConnectedWorkspaceStorageIssuer | None = None

    def assert_provisionable(self, connection: AwsAccountConnection) -> None:
        if self.connected is None:
            raise WorkspaceStorageError("connected cloud storage is not configured")
        try:
            self.connected.assert_provisionable(connection)
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc

    def provision(self, workspace: WorkspaceRecord) -> WorkspaceStorageConfig:
        if workspace.connection_id is not None:
            if self.connected is None:
                raise WorkspaceStorageError("connected cloud storage is not configured")
            with self.database.session() as session:
                connection = AwsAccountConnectionRepository(session).get(workspace.connection_id)
            if connection is None:
                raise WorkspaceStorageError("the workspace's connected account no longer exists")
            return self.connected.provision(workspace, connection)
        client = self.client
        if client is None:
            raise WorkspaceStorageError("workspace storage client settings are unavailable")
        settings = client.settings
        bucket = f"{settings.workspace_bucket_prefix}-{workspace.id}".replace("_", "-")
        try:
            client.create_bucket(bucket)
            client.validate_bucket_access(bucket)
            client.configure_workspace_bucket(bucket, public_origin=self.public_origin)
        except Exception as exc:
            raise WorkspaceStorageError(
                f"unable to create workspace storage bucket {bucket!r}"
            ) from exc
        return WorkspaceStorageConfig(
            backend="s3",
            bucket=bucket,
            endpoint_url=settings.endpoint_url or "",
            region=settings.region_name,
        )
