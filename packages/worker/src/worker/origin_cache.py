from __future__ import annotations

from worker.image_lifecycle import ImageArchiveRegistryConfig
from worker.tools import WorkspaceStorageCredentials


def image_archive_registry_config(
    credentials: WorkspaceStorageCredentials | None,
) -> ImageArchiveRegistryConfig:
    if credentials is None:
        return ImageArchiveRegistryConfig()
    return ImageArchiveRegistryConfig(
        bucket_name=credentials.bucket_name,
        region=credentials.region,
        endpoint_url=credentials.endpoint_url,
        force_path_style=credentials.force_path_style,
        has_access_key=credentials.access_key != "",
        has_secret_key=credentials.secret_key != "",
    )
