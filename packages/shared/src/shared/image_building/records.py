from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

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
    id: str = ""
    workspace_id: str = Field(min_length=1)
    image_id: str
    clip_version: int = 1
    archive_object_id: str = ""
    archive_object_key: str = ""
    archive_size_bytes: int = Field(default=0, ge=0)
    archive_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    aliases: list[str] = Field(default_factory=list)
    cleanup_claimed_at: datetime | None = None
    cleanup_completed_at: datetime | None = None

    @model_validator(mode="after")
    def require_complete_archive_identity(self) -> ImageRecord:
        archive_fields = (
            bool(self.archive_object_id),
            bool(self.archive_object_key),
            self.archive_size_bytes > 0,
            bool(self.archive_sha256),
        )
        if any(archive_fields) and not all(archive_fields):
            raise ValueError(
                "image archive identity requires object id, object key, positive size, and sha256"
            )
        return self

    @property
    def has_archive(self) -> bool:
        return bool(self.archive_object_id)


__all__ = [
    "BuildStatus",
    "ImageBuildPhase",
    "ImageBuildRecord",
    "ImageRecord",
]
