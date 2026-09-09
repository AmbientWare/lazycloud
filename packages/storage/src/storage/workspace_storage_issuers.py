from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.identity import WorkspaceStorageConfig
from shared.workspace_storage import (
    WorkspaceStorageGrant,
    WorkspaceStorageIssuer,
    WorkspaceStorageProvider,
)
from storage_client.s3 import S3ObjectStoreSettings


class WorkspaceStorageIssuerSettings(BaseSettings):
    issuer: WorkspaceStorageProvider | None = None

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_WORKSPACE_STORAGE_")


@dataclass(frozen=True, slots=True)
class StoredWorkspaceStorageIssuer:
    """Use only credentials supplied for the customer's own bucket."""

    def retire(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        """Customer-owned buckets outlive the workspace that used them."""

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        if not storage.access_key or not storage.secret_key or not storage.endpoint_url:
            raise ValueError("external workspace storage requires its own endpoint and key pair")
        return WorkspaceStorageGrant(
            endpoint_url=storage.endpoint_url,
            region=storage.region,
            bucket_name=storage.bucket or "",
            prefix=storage.key_prefix,
            force_path_style=storage.force_path_style,
            access_key=storage.access_key,
            secret_key=storage.secret_key,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceStorageRouter:
    managed: WorkspaceStorageIssuer

    def retire(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> None:
        if not storage.bucket or storage.access_key or storage.secret_key:
            return
        self.managed.retire(workspace_id=workspace_id, storage=storage)

    def issue(self, *, workspace_id: str, storage: WorkspaceStorageConfig) -> WorkspaceStorageGrant:
        if storage.access_key or storage.secret_key:
            return StoredWorkspaceStorageIssuer().issue(workspace_id=workspace_id, storage=storage)
        return self.managed.issue(workspace_id=workspace_id, storage=storage)


def external_workspace_storage_settings(storage: WorkspaceStorageConfig) -> S3ObjectStoreSettings:
    grant = StoredWorkspaceStorageIssuer().issue(workspace_id="", storage=storage)
    return S3ObjectStoreSettings(
        bucket=grant.bucket_name,
        endpoint_url=grant.endpoint_url,
        region_name=grant.region,
        access_key_id=grant.access_key,
        secret_access_key=grant.secret_key,
        session_token=grant.session_token,
        credential_expires_at=grant.expires_at,
        force_path_style=grant.force_path_style,
        workspace_bucket_prefix="",
    )


__all__ = [
    "StoredWorkspaceStorageIssuer",
    "WorkspaceStorageIssuerSettings",
    "WorkspaceStorageRouter",
    "external_workspace_storage_settings",
]
