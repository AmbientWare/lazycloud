from __future__ import annotations

from dataclasses import dataclass

from shared.identity import WorkspaceStorageConfig
from shared.workspace_storage import WorkspaceStorageGrant


@dataclass(frozen=True, slots=True)
class StoredWorkspaceStorageIssuer:
    """Hands back whatever credential the workspace record already holds.

    What every deployment did before an issuer existed, kept for storage a
    customer attached themselves: those are their credentials for their own
    bucket, so there is nothing for the platform to mint and no expiry to report.
    """

    def provision(self, *, workspace_id: str, bucket: str) -> dict[str, str]:
        _ = (workspace_id, bucket)
        return {}

    def issue(
        self,
        *,
        workspace_id: str,
        storage: WorkspaceStorageConfig,
    ) -> WorkspaceStorageGrant:
        _ = workspace_id
        return WorkspaceStorageGrant(
            endpoint_url=storage.endpoint_url,
            region=storage.region,
            bucket_name=storage.bucket or "",
            prefix=storage.key_prefix,
            force_path_style=storage.force_path_style,
            access_key=storage.access_key,
            secret_key=storage.secret_key,
        )


__all__ = ["StoredWorkspaceStorageIssuer"]
