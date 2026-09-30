from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.disks import DiskRecord, DiskStatus, DiskWorkload
from shared.http.base import HttpModel


class DiskResponse(HttpModel):
    id: str
    name: str
    size_bytes: int = Field(gt=0)
    status: DiskStatus
    generation: int = Field(ge=0)
    """Newest published generation; 0 means nothing was ever published."""

    stored_bytes: int = Field(ge=0)
    """Bytes the disk's layers occupy in the workspace bucket."""

    holder_container_id: str = ""
    workload: DiskWorkload | None = None
    """The workload whose container last asked for the disk; null once it is gone."""

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: DiskRecord, workload: DiskWorkload | None) -> DiskResponse:
        return cls(
            id=record.id,
            name=record.name,
            size_bytes=record.size_bytes,
            status=record.status,
            generation=record.generation,
            stored_bytes=record.stored_bytes,
            holder_container_id=record.holder_container_id,
            workload=workload,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class DiskListResponse(HttpModel):
    data: list[DiskResponse] = Field(default_factory=list)
    next: str = ""
    """Pass as `cursor` for the next page; empty on the last one."""


__all__ = ["DiskListResponse", "DiskResponse"]
