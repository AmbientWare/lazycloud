from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.bytes_transport import EncodedBytesBody, encode_bytes
from shared.deployments import DeploymentKind
from shared.http.base import HttpModel


class ResourceWorkloadReference(HttpModel):
    app_id: str
    app_name: str
    name: str
    kind: DeploymentKind
    versions: list[int] = Field(default_factory=list)
    active_versions: list[int] = Field(default_factory=list)


class ObjectCreateRequest(EncodedBytesBody):
    bucket: str = "default"
    key: str = "object"
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = Field(default_factory=dict)


class ObjectResponse(HttpModel):
    id: str
    bucket: str
    key: str
    path: str
    size: int
    sha256: str
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ObjectListResponse(HttpModel):
    objects: list[ObjectResponse] = Field(default_factory=list)


class ObjectContentResponse(EncodedBytesBody):
    object: ObjectResponse

    @classmethod
    def from_content(cls, *, record: ObjectResponse, data: bytes) -> ObjectContentResponse:
        return cls(object=record, value_base64=encode_bytes(data))


class CacheCreateRequest(EncodedBytesBody):
    namespace: str
    key: str


class CacheEntryResponse(HttpModel):
    key: str
    size: int
    sha256: str
    hits: int = 0
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CacheEntryListResponse(HttpModel):
    entries: list[CacheEntryResponse] = Field(default_factory=list)


class CacheContentResponse(EncodedBytesBody):
    entry: CacheEntryResponse

    @classmethod
    def from_content(cls, *, entry: CacheEntryResponse, data: bytes) -> CacheContentResponse:
        return cls(entry=entry, value_base64=encode_bytes(data))


__all__ = [
    "CacheContentResponse",
    "CacheCreateRequest",
    "CacheEntryListResponse",
    "CacheEntryResponse",
    "ObjectContentResponse",
    "ObjectCreateRequest",
    "ObjectListResponse",
    "ObjectResponse",
    "ResourceWorkloadReference",
]
