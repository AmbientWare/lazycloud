from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from shared.bytes_transport import encode_bytes
from shared.http.outputs import (
    OutputPublicUrlRequest,
    OutputPublicUrlResponse,
    OutputSaveBody,
    OutputSaveResponse,
    OutputStatRequest,
    OutputStatResponse,
)
from shared.http_transport import HttpChannel


class OutputControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass
class OutputControlClient:
    channel: OutputControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> OutputControlClient:
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
    ) -> OutputSaveResponse:
        body = OutputSaveBody(
            task_id=task_id,
            filename=filename,
            content_type=content_type,
            value_base64=encode_bytes(content),
        )
        return OutputSaveResponse.model_validate(
            self.channel.post(
                self._path("save"),
                body.model_dump(mode="json"),
            )
        )

    def output_save_stream(
        self,
        task_id: str,
        filename: str,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> OutputSaveResponse:
        content = b"".join(chunks)
        return self.save(task_id, filename, content, content_type=content_type)

    def stat(self, output_id: str, task_id: str, filename: str) -> OutputStatResponse:
        return self.output_stat(OutputStatRequest(id=output_id, task_id=task_id, filename=filename))

    def output_stat(self, request: OutputStatRequest) -> OutputStatResponse:
        return OutputStatResponse.model_validate(
            self.channel.post(
                self._path("stat"),
                request.model_dump(mode="json"),
            )
        )

    def public_url(
        self,
        output_id: str,
        task_id: str,
        filename: str,
        *,
        expires: int = 3600,
        gateway_external_url: str = "http://127.0.0.1:9000",
    ) -> OutputPublicUrlResponse:
        return self.output_public_url(
            OutputPublicUrlRequest(
                id=output_id,
                task_id=task_id,
                filename=filename,
                expires=expires,
                gateway_external_url=gateway_external_url,
            )
        )

    def output_public_url(self, request: OutputPublicUrlRequest) -> OutputPublicUrlResponse:
        return OutputPublicUrlResponse.model_validate(
            self.channel.post(
                self._path("public-url"),
                request.model_dump(mode="json"),
            )
        )

    def _path(self, suffix: str) -> str:
        return f"/api/v1/outputs/{suffix}"


__all__ = [
    "OutputControlChannel",
    "OutputControlClient",
]
