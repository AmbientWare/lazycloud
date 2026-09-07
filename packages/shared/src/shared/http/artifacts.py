from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from shared.artifacts import ArtifactRetentionSource
from shared.bytes_transport import EncodedBytesBody
from shared.http.base import HttpModel

DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS = 3600


class ArtifactSaveBody(EncodedBytesBody):
    task_id: str
    filename: str
    content_type: str = "application/octet-stream"
    retention_seconds: int | None = Field(default=None, gt=0, strict=True)


class ArtifactSaveResponse(HttpModel):
    id: str = ""
    expires_at: datetime | None = None
    retention_source: ArtifactRetentionSource = ArtifactRetentionSource.Workspace
    retention_seconds: int | None = Field(default=None, gt=0)


class ArtifactStatRequest(HttpModel):
    id: str
    task_id: str
    filename: str


class ArtifactStat(HttpModel):
    mode: str = "0644"
    size: int = Field(default=0, ge=0)
    atime: datetime | None = None
    mtime: datetime | None = None


class ArtifactStatResponse(HttpModel):
    stat: ArtifactStat | None = None


class ArtifactPublicUrlRequest(HttpModel):
    id: str
    task_id: str
    filename: str
    expires: int = Field(default=DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS, ge=0)


class ArtifactPublicUrlResponse(HttpModel):
    public_url: str = ""


class ArtifactSummary(HttpModel):
    """One saved artifact, enough for a reader to list and render it."""

    id: str
    task_id: str
    filename: str
    content_type: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    created_at: datetime | None = None
    app_id: str | None = None
    app_name: str = ""
    expires_at: datetime | None = None
    retention_source: ArtifactRetentionSource = ArtifactRetentionSource.Workspace
    retention_seconds: int | None = Field(default=None, gt=0)
    deleting: bool = False
    deletion_failed: bool = False


class ArtifactRetentionUpdate(HttpModel):
    retention_seconds: int | None = Field(gt=0, strict=True)


class ArtifactRetentionPolicy(HttpModel):
    retention_seconds: int | None = Field(default=None, gt=0)


class ArtifactRetentionSelection(ArtifactRetentionUpdate):
    ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("ids")
    @classmethod
    def valid_ids(cls, values: list[str]) -> list[str]:
        return [str(UUID(value)) for value in values]


class ArtifactRetentionPreview(HttpModel):
    data: list[ArtifactSummary]
    total_bytes: int = Field(ge=0)


class ArtifactStorageSummary(HttpModel):
    count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    estimated_monthly_nanos: int | None = Field(default=None, ge=0)
    accrued_nanos: int = Field(ge=0)
    accrued_since: datetime
    retention_seconds: int | None = Field(default=None, gt=0)


class ArtifactListResponse(HttpModel):
    data: list[ArtifactSummary] = Field(default_factory=list)
    next: str = ""


__all__ = [
    "DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS",
    "ArtifactListResponse",
    "ArtifactPublicUrlRequest",
    "ArtifactPublicUrlResponse",
    "ArtifactRetentionPolicy",
    "ArtifactRetentionPreview",
    "ArtifactRetentionSelection",
    "ArtifactRetentionUpdate",
    "ArtifactSaveBody",
    "ArtifactSaveResponse",
    "ArtifactStat",
    "ArtifactStatRequest",
    "ArtifactStatResponse",
    "ArtifactStorageSummary",
    "ArtifactSummary",
]
