from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue
from shared.bytes_transport import encode_bytes
from shared.http.artifacts import (
    ArtifactListResponse,
    ArtifactPublicUrlRequest,
    ArtifactPublicUrlResponse,
    ArtifactSaveBody,
    ArtifactSaveResponse,
    ArtifactStatRequest,
    ArtifactStatResponse,
    ArtifactStorageSummary,
)
from shared.http_transport import HttpChannel


class ArtifactControlChannel(Protocol):
    def post(self, path: str, payload: Mapping[str, JsonValue] | None = None) -> JsonValue: ...
    def get(self, path: str) -> JsonValue: ...
    def delete(self, path: str) -> JsonValue: ...


@dataclass
class ArtifactControlClient:
    channel: ArtifactControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ArtifactControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def save(
        self,
        task_id: str,
        filename: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> ArtifactSaveResponse:
        body = ArtifactSaveBody(
            task_id=task_id,
            filename=filename,
            content_type=content_type,
            value_base64=encode_bytes(content),
        )
        return ArtifactSaveResponse.model_validate(
            self.channel.post(
                self._path("save"),
                body.model_dump(mode="json", exclude_unset=True),
            )
        )

    def artifact_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> ArtifactSaveResponse:
        content = b"".join(chunks)
        return self.save(
            task_id,
            filename,
            content,
            content_type=content_type,
        )

    def delete(self, artifact_id: str) -> None:
        self.channel.delete(self._path(artifact_id))

    def list(
        self, *, task_id: str | None = None, search: str = "", cursor: str = ""
    ) -> ArtifactListResponse:
        from urllib.parse import urlencode

        query = urlencode(
            {
                "workspace": self.workspace,
                "search": search,
                "cursor": cursor,
                **({"task_id": task_id} if task_id else {}),
            }
        )
        return ArtifactListResponse.model_validate(self.channel.get(f"/api/v1/artifacts?{query}"))

    def summary(self) -> ArtifactStorageSummary:
        return ArtifactStorageSummary.model_validate(self.channel.get(self._path("summary")))

    def stat(self, artifact_id: str, task_id: str, filename: str) -> ArtifactStatResponse:
        return self.artifact_stat(
            ArtifactStatRequest(id=artifact_id, task_id=task_id, filename=filename)
        )

    def artifact_stat(self, request: ArtifactStatRequest) -> ArtifactStatResponse:
        return ArtifactStatResponse.model_validate(
            self.channel.post(
                self._path("stat"),
                request.model_dump(mode="json"),
            )
        )

    def public_url(
        self,
        artifact_id: str,
        task_id: str,
        filename: str,
        *,
        expires: int = 3600,
    ) -> ArtifactPublicUrlResponse:
        return self.artifact_public_url(
            ArtifactPublicUrlRequest(
                id=artifact_id,
                task_id=task_id,
                filename=filename,
                expires=expires,
            )
        )

    def artifact_public_url(self, request: ArtifactPublicUrlRequest) -> ArtifactPublicUrlResponse:
        return ArtifactPublicUrlResponse.model_validate(
            self.channel.post(
                self._path("public-url"),
                request.model_dump(mode="json"),
            )
        )

    def _path(self, suffix: str) -> str:
        from urllib.parse import quote

        return f"/api/v1/artifacts/{suffix}?workspace={quote(self.workspace, safe='')}"


__all__ = [
    "ArtifactControlChannel",
    "ArtifactControlClient",
]
