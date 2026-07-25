from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from shared.contracts import ContractModel
from shared.timestamps import utc_now


class ObjectWriteCommand(ContractModel):
    """Validated immutable intent for one object location write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    key: str
    path: str
    size: int = Field(ge=0)
    sha256: str
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = Field(default_factory=dict)


class ObjectRecord(ContractModel):
    id: str
    bucket: str
    key: str
    path: str
    size: int = Field(ge=0)
    sha256: str
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    write_claim_id: str = ""
    write_claimed_at: datetime | None = None
    write_created: bool = False
    write_target: ObjectWriteCommand | None = None
    cleanup_kind: str = ""
    cleanup_claimed_at: datetime | None = None
