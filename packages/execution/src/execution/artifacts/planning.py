from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field
from shared.contracts import ContractModel
from shared.http.artifacts import DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS

DEFAULT_ARTIFACTS_PREFIX = "artifacts"


class ArtifactPublicUrlStatus(StrEnum):
    PresignedObjectUrl = "presigned-object-url"
    InvalidRequest = "invalid-request"


class ArtifactPathPlan(ContractModel):
    workspace_name: str
    stub_external_id: str
    task_external_id: str
    artifact_id: str
    filename: str
    storage_prefix: str
    storage_key: str


class ArtifactStatPlan(ContractModel):
    artifact_id: str
    task_id: str
    filename: str
    mode: str
    size: int = Field(ge=0)
    accessed_at: datetime | None = None
    modified_at: datetime | None = None


class ArtifactPublicUrlPlan(ContractModel):
    status: ArtifactPublicUrlStatus
    artifact_id: str
    cache_key: str
    target_path: str
    public_url: str
    expires_seconds: int = Field(ge=0)

    @property
    def ok(self) -> bool:
        return self.status is ArtifactPublicUrlStatus.PresignedObjectUrl


def artifact_public_url_key(artifact_id: str) -> str:
    return f"artifact:{artifact_id}"


def artifact_storage_prefix(stub_external_id: str, task_external_id: str) -> str:
    return _join(DEFAULT_ARTIFACTS_PREFIX, stub_external_id, task_external_id)


def plan_artifact_path(
    workspace_name: str,
    stub_external_id: str,
    task_external_id: str,
    artifact_id: str,
    filename: str,
) -> ArtifactPathPlan:
    safe_name = _safe_filename(filename)
    storage_prefix = artifact_storage_prefix(stub_external_id, task_external_id)
    return ArtifactPathPlan(
        workspace_name=workspace_name,
        stub_external_id=stub_external_id,
        task_external_id=task_external_id,
        artifact_id=artifact_id,
        filename=safe_name,
        storage_prefix=storage_prefix,
        storage_key=_join(storage_prefix, artifact_id, safe_name),
    )


def plan_artifact_public_url(
    *,
    artifact_id: str,
    target_path: str,
    expires_seconds: int = DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS,
    presigned_url: str = "",
) -> ArtifactPublicUrlPlan:
    """Plan the short-lived direct link to an artifact's stored object.

    Artifacts live in the workspace's own object storage, so the link is always
    a presigned object URL; there is no second addressing scheme to choose
    between.
    """
    status = (
        ArtifactPublicUrlStatus.PresignedObjectUrl
        if presigned_url
        else ArtifactPublicUrlStatus.InvalidRequest
    )
    return ArtifactPublicUrlPlan(
        status=status,
        artifact_id=artifact_id,
        cache_key=artifact_public_url_key(artifact_id),
        target_path=target_path,
        public_url=presigned_url,
        expires_seconds=expires_seconds,
    )


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").rstrip("/")
    name = PurePosixPath(normalized).name
    return name or "artifact"


def _join(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part.strip("/")]
    prefix = "/" if parts and parts[0].startswith("/") else ""
    return prefix + "/".join(cleaned)
