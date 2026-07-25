from __future__ import annotations

from enum import StrEnum

from pydantic import field_validator
from shared.contracts import ContractModel

DEFAULT_CONTENT_CHUNK_BYTES = 1024 * 1024


class CacheContentReadStatus(StrEnum):
    Hit = "hit"
    Miss = "miss"
    Unavailable = "unavailable"
    ShortRead = "short-read"
    Corrupt = "corrupt"
    Error = "error"


class CacheContentStoreStatus(StrEnum):
    Stored = "stored"
    AlreadyPresent = "already-present"
    HashMismatch = "hash-mismatch"
    SourceMissing = "source-missing"
    Unavailable = "unavailable"
    Error = "error"


class CacheContentCompletenessStatus(StrEnum):
    Complete = "complete"
    Missing = "missing"
    SizeMismatch = "size-mismatch"
    Unavailable = "unavailable"


class CacheContentMetadata(ContractModel):
    content_hash: str
    size_bytes: int
    cache_path: str = ""
    complete: bool = True


class CacheContentReadRequest(ContractModel):
    content_hash: str
    offset: int = 0
    length: int = DEFAULT_CONTENT_CHUNK_BYTES
    routing_key: str = ""

    @field_validator("offset", "length")
    @classmethod
    def non_negative_range(cls, value: int) -> int:
        if value < 0:
            msg = "cache content read range values cannot be negative"
            raise ValueError(msg)
        return value


class CacheContentReadResult(ContractModel):
    status: CacheContentReadStatus
    content_hash: str
    data: bytes = b""
    offset: int = 0
    length: int = 0
    reason: str = ""


class CacheContentStoreResult(ContractModel):
    status: CacheContentStoreStatus
    content_hash: str = ""
    actual_hash: str = ""
    cache_path: str = ""
    size_bytes: int = 0
    reason: str = ""

    @property
    def stored(self) -> bool:
        return self.status in {
            CacheContentStoreStatus.Stored,
            CacheContentStoreStatus.AlreadyPresent,
        }


class CacheContentCompleteness(ContractModel):
    status: CacheContentCompletenessStatus
    content_hash: str
    size_bytes: int = 0
    expected_size_bytes: int = 0
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is CacheContentCompletenessStatus.Complete
