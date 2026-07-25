from __future__ import annotations

from pydantic import Field

from shared.bytes_transport import EncodedBytesBody, decode_bytes, encode_bytes
from shared.http.base import HttpModel

MAX_MAP_TTL_SECONDS = 7 * 24 * 60 * 60


class MapSetBody(EncodedBytesBody):
    key: str
    ttl_seconds: int = Field(default=MAX_MAP_TTL_SECONDS, ge=0)


class MapKeyBody(HttpModel):
    key: str


class MapSetResponse(HttpModel):
    pass


class MapGetResponse(EncodedBytesBody):
    pass


class MapDeleteResponse(HttpModel):
    pass


class MapCountResponse(HttpModel):
    count: int = 0


class MapKeysResponse(HttpModel):
    keys: list[str] = Field(default_factory=list)


class SimpleQueuePutBody(EncodedBytesBody):
    pass


class SimpleQueuePutResponse(HttpModel):
    pass


class SimpleQueuePopResponse(EncodedBytesBody):
    pass


class SimpleQueuePeekResponse(EncodedBytesBody):
    pass


class SimpleQueueEmptyResponse(HttpModel):
    empty: bool


class SimpleQueueSizeResponse(HttpModel):
    size: int = 0


class SimpleQueueInfo(HttpModel):
    name: str
    size: int = 0
    oldest_message_age_seconds: float | None = Field(default=None, ge=0)
    put_rate_per_minute: int = Field(default=0, ge=0)


class SimpleQueueListResponse(HttpModel):
    queues: list[SimpleQueueInfo] = Field(default_factory=list)


class MapCollectionInfo(HttpModel):
    name: str
    count: int = 0
    size_bytes: int = Field(default=0, ge=0)
    expiring_keys: int = Field(default=0, ge=0)
    nearest_expiry_seconds: int | None = Field(default=None, ge=0)


class MapCollectionListResponse(HttpModel):
    maps: list[MapCollectionInfo] = Field(default_factory=list)


__all__ = [
    "MAX_MAP_TTL_SECONDS",
    "EncodedBytesBody",
    "MapCollectionInfo",
    "MapCollectionListResponse",
    "MapCountResponse",
    "MapDeleteResponse",
    "MapGetResponse",
    "MapKeyBody",
    "MapKeysResponse",
    "MapSetBody",
    "MapSetResponse",
    "SimpleQueueEmptyResponse",
    "SimpleQueueInfo",
    "SimpleQueueListResponse",
    "SimpleQueuePeekResponse",
    "SimpleQueuePopResponse",
    "SimpleQueuePutBody",
    "SimpleQueuePutResponse",
    "SimpleQueueSizeResponse",
    "decode_bytes",
    "encode_bytes",
]
