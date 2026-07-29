from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.image_building.authoring import ImageSpec
from shared.timestamps import utc_now


class BuildStatus(StringEnum):
    Pending = "pending"
    Running = "running"
    Complete = "complete"
    Failed = "failed"
    Cancelled = "cancelled"
    Timeout = "timeout"


class ImageBuildPhase(StringEnum):
    Verify = "verify"
    Planning = "planning"
    Submitted = "submitted"
    Manifest = "manifest"
    Complete = "complete"
    Failed = "failed"
    Reused = "reused"


class ImageBuildRecord(ContractModel):
    id: str
    image: ImageSpec
    fingerprint: str
    image_id: str | None = None
    cache_key: str | None = None
    dockerfile: str | None = None
    context_digest: str | None = None
    status: BuildStatus = BuildStatus.Pending
    phase: ImageBuildPhase = ImageBuildPhase.Planning
    tag: str | None = None
    manifest_path: str | None = None
    published_ref: str | None = None
    artifact_path: str | None = None
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cleanup_claimed_at: datetime | None = None


class ImageRecord(ContractModel):
    """A workspace's authorization to use an image. Archive bytes are global."""

    id: str = ""
    workspace_id: str = Field(min_length=1)
    image_id: str
    clip_version: int = 1
    aliases: list[str] = Field(default_factory=list)
    cleanup_claimed_at: datetime | None = None
    cleanup_completed_at: datetime | None = None


class ImageArchiveRecord(ContractModel):
    """The one archive for an image id, shared by every workspace authorized for it."""

    id: str = ""
    image_id: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    object_key: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleanup_claimed_at: datetime | None = None


__all__ = [
    "BuildStatus",
    "ImageArchiveRecord",
    "ImageBuildPhase",
    "ImageBuildRecord",
    "ImageRecord",
]
