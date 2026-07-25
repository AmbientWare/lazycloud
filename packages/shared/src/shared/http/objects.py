from __future__ import annotations

import re

from pydantic import Field, field_validator

from shared.app_identity import WORKSPACE_OBJECT_BUCKET, WORKSPACE_UPLOAD_BUCKETS
from shared.http.base import HttpModel


class ObjectMetadata(HttpModel):
    name: str = Field(default="", max_length=1024)
    size: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def name_fits_object_store_key(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("object name exceeds the 1024-byte limit")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("object name contains a control character")
        return value


class HeadObjectRequest(HttpModel):
    hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    bucket: str = Field(default=WORKSPACE_OBJECT_BUCKET, min_length=1, max_length=63)

    @field_validator("bucket")
    @classmethod
    def bucket_is_server_owned(cls, value: str) -> str:
        return _workspace_upload_bucket(value)


class HeadObjectResponse(HttpModel):
    exists: bool = False
    object_id: str = ""
    object_metadata: ObjectMetadata = Field(default_factory=ObjectMetadata)


class PutObjectRequest(HttpModel):
    object_metadata: ObjectMetadata = Field(default_factory=ObjectMetadata)
    hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    bucket: str = Field(default=WORKSPACE_OBJECT_BUCKET, min_length=1, max_length=63)
    overwrite: bool = False
    content_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("bucket")
    @classmethod
    def bucket_is_server_owned(cls, value: str) -> str:
        return _workspace_upload_bucket(value)

    @field_validator("content_type")
    @classmethod
    def content_type_is_header_safe(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("object content type contains a control character")
        return value

    @field_validator("metadata")
    @classmethod
    def metadata_fits_object_store_headers(cls, value: dict[str, str]) -> dict[str, str]:
        total_bytes = 0
        for key, item in value.items():
            if _METADATA_KEY_PATTERN.fullmatch(key) is None:
                raise ValueError(f"invalid object metadata key: {key}")
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise ValueError(f"invalid object metadata value: {key}")
            total_bytes += len(key.encode("utf-8")) + len(item.encode("utf-8"))
        if total_bytes > _MAX_METADATA_BYTES:
            raise ValueError("object metadata exceeds the 2048-byte limit")
        return value


class PutObjectResponse(HttpModel):
    object_id: str = Field(min_length=1)


_METADATA_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_MAX_METADATA_BYTES = 2048


def _workspace_upload_bucket(value: str) -> str:
    if value not in WORKSPACE_UPLOAD_BUCKETS:
        raise ValueError("object upload bucket is not available")
    return value


__all__ = [
    "HeadObjectRequest",
    "HeadObjectResponse",
    "ObjectMetadata",
    "PutObjectRequest",
    "PutObjectResponse",
]
