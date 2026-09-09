from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, Field

from shared.contracts import ContractModel


class StorageRequestClass(StrEnum):
    Read = "read"
    Write = "write"
    Delete = "delete"
    Other = "other"


class StorageTransferEvidence(StrEnum):
    SameRegion = "same_region"
    OtherRegion = "other_region"
    Unknown = "unknown"


class StorageAccessObservation(ContractModel):
    provider: str = Field(min_length=1, max_length=32)
    bucket: str = Field(min_length=1, max_length=255)
    key: str = Field(max_length=2048)
    request_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    request_class: StorageRequestClass
    occurred_at: AwareDatetime
    status_code: int = Field(ge=100, le=599)
    response_bytes: int | None = Field(default=None, ge=0)
    source_region: str = Field(default="", max_length=64)
    transfer_evidence: StorageTransferEvidence


@dataclass(frozen=True, slots=True)
class StorageAccessDelivery:
    keys: tuple[str, ...]
    receipt: str = field(repr=False)


class StorageAccessSource(Protocol):
    def receive(self) -> StorageAccessDelivery | None: ...

    def read(self, key: str) -> Iterator[StorageAccessObservation]: ...

    def renew(self, delivery: StorageAccessDelivery) -> None: ...

    def acknowledge(self, delivery: StorageAccessDelivery) -> None: ...

    def close(self) -> None: ...
