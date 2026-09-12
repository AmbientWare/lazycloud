from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from shared.enums import StringEnum
from shared.http.base import HttpModel

PREVIEW_LEASE_SECONDS = 60
PREVIEW_HEARTBEAT_SECONDS = 20


class PreviewSessionStatus(StringEnum):
    Active = "active"
    Stopped = "stopped"
    Expired = "expired"


class CreatePreviewRequest(HttpModel):
    stub_id: str
    timeout: int = Field(default=0, ge=0)

    @field_validator("stub_id")
    @classmethod
    def source_identifier(cls, value: str) -> str:
        return str(UUID(value))


class PreviewSessionResponse(HttpModel):
    id: str
    workspace_id: str
    source_stub_id: str | None
    execution_stub_id: str | None
    container_id: str | None
    status: PreviewSessionStatus
    public: bool
    created_at: datetime
    expires_at: datetime | None
    ended_at: datetime | None = None


def preview_invocation_path(preview_id: str, *, public: bool) -> str:
    scope = "public/" if public else ""
    return f"/api/v1/previews/{scope}{preview_id}/invoke"
