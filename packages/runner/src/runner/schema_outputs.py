from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from pydantic import JsonValue
from shared.bytes_transport import encode_bytes
from shared.http.artifacts import (
    ArtifactPublicUrlRequest,
    ArtifactPublicUrlResponse,
    ArtifactSaveBody,
    ArtifactSaveResponse,
)
from shared.schema import MediaOutput


class ArtifactOutputChannel(Protocol):
    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue: ...


@dataclass(frozen=True)
class ArtifactOutputPublisher:
    channel: ArtifactOutputChannel
    workspace: str
    task_id: str

    def __call__(self, media: MediaOutput) -> str:
        if not self.task_id or not self.workspace:
            raise RuntimeError("File and Image outputs require a task and workspace")
        filename = f"output-{uuid4().hex}{media.suffix}"
        query = f"workspace={quote(self.workspace, safe='')}"
        saved = ArtifactSaveResponse.model_validate(
            self.channel.post(
                f"/api/v1/artifacts/save?{query}",
                ArtifactSaveBody(
                    task_id=self.task_id,
                    filename=filename,
                    content_type=media.content_type,
                    value_base64=encode_bytes(media.data),
                ).model_dump(mode="json"),
            )
        )
        return ArtifactPublicUrlResponse.model_validate(
            self.channel.post(
                f"/api/v1/artifacts/public-url?{query}",
                ArtifactPublicUrlRequest(
                    id=saved.id,
                    task_id=self.task_id,
                    filename=filename,
                ).model_dump(mode="json"),
            )
        ).public_url


__all__ = ["ArtifactOutputPublisher"]
