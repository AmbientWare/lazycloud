from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.bytes_transport import EncodedBytesBody
from shared.http.base import HttpModel

DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS = 3600


class OutputSaveBody(EncodedBytesBody):
    task_id: str
    filename: str
    content_type: str = "application/octet-stream"


class OutputSaveResponse(HttpModel):
    id: str = ""


class OutputStatRequest(HttpModel):
    id: str
    task_id: str
    filename: str


class OutputStat(HttpModel):
    mode: str = "0644"
    size: int = Field(default=0, ge=0)
    atime: datetime | None = None
    mtime: datetime | None = None


class OutputStatResponse(HttpModel):
    stat: OutputStat | None = None


class OutputPublicUrlRequest(HttpModel):
    id: str
    task_id: str
    filename: str
    expires: int = Field(default=DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS, ge=0)
    gateway_external_url: str = "http://127.0.0.1:9000"


class OutputPublicUrlResponse(HttpModel):
    public_url: str = ""


__all__ = [
    "DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS",
    "OutputPublicUrlRequest",
    "OutputPublicUrlResponse",
    "OutputSaveBody",
    "OutputSaveResponse",
    "OutputStat",
    "OutputStatRequest",
    "OutputStatResponse",
]
