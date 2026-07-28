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
    gateway_external_url: str = "http://127.0.0.1:9000"


class ArtifactPublicUrlResponse(HttpModel):
    public_url: str = ""


__all__ = [
    "DEFAULT_ARTIFACT_PUBLIC_URL_EXPIRES_SECONDS",
    "ArtifactPublicUrlRequest",
    "ArtifactPublicUrlResponse",
    "ArtifactSaveBody",
    "ArtifactSaveResponse",
    "ArtifactStat",
    "ArtifactStatRequest",
    "ArtifactStatResponse",
]
