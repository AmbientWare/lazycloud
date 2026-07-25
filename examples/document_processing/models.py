"""HTTP and worker result models for the document-processing example."""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class OcrTaskResult(TypedDict):
    document_id: str
    page_count: int
    result_path: str


class OcrResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    page_count: int = Field(ge=1)
    text: str


class UploadAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_token: str
    status_endpoint: str = "/api/jobs/status"
    result_endpoint: str = "/api/jobs/result"


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "pending",
        "running",
        "retry",
        "complete",
        "failed",
        "expired",
        "timeout",
        "cancelled",
    ]
    ready: bool
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
