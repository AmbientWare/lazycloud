from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum


class InheritRetention(Enum):
    Workspace = "workspace"


class ArtifactRetentionSource(StringEnum):
    Workspace = "workspace"
    Explicit = "explicit"


class ArtifactObjectFields(ContractModel):
    artifact_task_id: str | None = None
    artifact_app_id: str | None = None
    artifact_app_name: str = ""
    artifact_filename: str = ""
    artifact_retention_source: ArtifactRetentionSource = ArtifactRetentionSource.Workspace
    artifact_retention_seconds: int | None = Field(default=None, gt=0)
    artifact_stored_at: datetime | None = None
    artifact_expires_at: datetime | None = None
    artifact_metered_at: datetime | None = None
    artifact_deletion_failed: bool = False


ARTIFACT_STORAGE_SUBJECT = "artifact_storage"
