from __future__ import annotations

from enum import StrEnum

from shared.contracts import ContractModel


class ObjectBackendKind(StrEnum):
    S3Compatible = "s3-compatible"


class ObjectLocation(ContractModel):
    backend: ObjectBackendKind
    bucket: str
    key: str


class RangeRequest(ContractModel):
    start: int = 0
    end: int | None = None

    def header_value(self) -> str:
        suffix = "" if self.end is None else str(self.end)
        return f"bytes={self.start}-{suffix}"
