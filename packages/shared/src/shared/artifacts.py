from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.contracts import ContractModel


class ArtifactObjectFields(ContractModel):
    artifact_task_id: str | None = None
    artifact_app_id: str | None = None
    artifact_app_name: str = ""
    artifact_filename: str = ""
    artifact_retention_seconds: int | None = Field(default=None, gt=0)
    artifact_stored_at: datetime | None = None
    artifact_expires_at: datetime | None = None
    artifact_metered_at: datetime | None = None
    artifact_deletion_failed: bool = False

    @model_validator(mode="after")
    def require_artifact_retention(self) -> ArtifactObjectFields:
        if self.artifact_task_id is not None and self.artifact_retention_seconds is None:
            raise ValueError("task artifacts require plan retention")
        return self


ARTIFACT_STORAGE_SUBJECT = "artifact_storage"
