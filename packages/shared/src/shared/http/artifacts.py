from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.bytes_transport import EncodedBytesBody
from shared.http.base import HttpModel

DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS = 3600


class ArtifactSaveBody(EncodedBytesBody):
    task_id: str
    filename: str
    content_type: str = "application/octet-stream"


class ArtifactSaveResponse(HttpModel):
    id: str = ""
    expires_at: datetime


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
    expires_at: datetime
    deleting: bool = False
    deletion_failed: bool = False


class ArtifactStorageSummary(HttpModel):
    count: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    estimated_monthly_nanos: int | None = Field(default=None, ge=0)
    accrued_nanos: int = Field(ge=0)
    accrued_since: datetime
    retention_seconds: int = Field(gt=0)


class ArtifactListResponse(HttpModel):
    data: list[ArtifactSummary] = Field(default_factory=list)
    next: str = ""


__all__ = [
    "DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS",
    "ArtifactListResponse",
    "ArtifactPublicUrlRequest",
    "ArtifactPublicUrlResponse",
    "ArtifactSaveBody",
    "ArtifactSaveResponse",
    "ArtifactStat",
    "ArtifactStatRequest",
    "ArtifactStatResponse",
    "ArtifactStorageSummary",
    "ArtifactSummary",
]
