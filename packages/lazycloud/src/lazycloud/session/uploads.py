from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, TypeAlias
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError
from shared.http.objects import (
    BeginObjectUploadResponse,
    ObjectMetadata,
    PutObjectRequest,
    PutObjectResponse,
)
from shared.http_transport import HttpChannel

from lazycloud.terminal import ProgressCallback

DEFAULT_OBJECT_UPLOAD_TIMEOUT_SECONDS = 300.0


def object_upload_timeout_seconds(timeout_seconds: float) -> float:
    return max(timeout_seconds, DEFAULT_OBJECT_UPLOAD_TIMEOUT_SECONDS)


def stream_object_bytes(
    *,
    channel: HttpChannel,
    workspace: str,
    data: bytes,
    name: str,
    object_hash: str,
    bucket: str,
    overwrite: bool,
    content_type: str,
    metadata: dict[str, str] | None,
    timeout_seconds: float,
    progress: ProgressCallback | None,
    chunk_size: int = 1024 * 1024,
) -> PutObjectResponse:
    """Upload bytes directly to storage and validate the API's committed object response."""
    body = _ProgressBytesReader(data, progress=progress, chunk_size=chunk_size)
    return _stream_object(
        channel=channel,
        workspace=workspace,
        body=body,
        size=len(data),
        name=name,
        object_hash=object_hash,
        bucket=bucket,
        overwrite=overwrite,
        content_type=content_type,
        metadata=metadata,
        timeout_seconds=timeout_seconds,
    )


def stream_object_file(
    *,
    channel: HttpChannel,
    workspace: str,
    source: str | Path,
    size: int,
    name: str,
    object_hash: str,
    bucket: str,
    overwrite: bool,
    content_type: str,
    metadata: dict[str, str] | None,
    timeout_seconds: float,
    progress: ProgressCallback | None,
    chunk_size: int = 1024 * 1024,
) -> PutObjectResponse:
    """Stream one file without retaining a second full copy in SDK memory."""
    source_path = Path(source).expanduser().resolve()
    with source_path.open("rb") as stream:
        body = _ProgressFileReader(stream, progress=progress, chunk_size=chunk_size)
        return _stream_object(
            channel=channel,
            workspace=workspace,
            body=body,
            size=size,
            name=name,
            object_hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=content_type,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
        )


def _stream_object(
    *,
    channel: HttpChannel,
    workspace: str,
    body: _ProgressReader,
    size: int,
    name: str,
    object_hash: str,
    bucket: str,
    overwrite: bool,
    content_type: str,
    metadata: dict[str, str] | None,
    timeout_seconds: float,
) -> PutObjectResponse:
    upload = PutObjectRequest(
        object_metadata=ObjectMetadata(name=name, size=size),
        hash=object_hash,
        bucket=bucket,
        overwrite=overwrite,
        content_type=content_type,
        metadata=metadata or {},
    )
    scope = urlencode({"workspace": workspace})
    try:
        prepared = BeginObjectUploadResponse.model_validate(
            channel.post(
                f"/gateway/objects/uploads?{scope}",
                upload.model_dump(mode="json"),
            )
        )
    except ValidationError as exc:
        raise HttpResponseDecodeError("object upload response contained invalid JSON") from exc
    if prepared.upload is None:
        body.finish(size)
        return PutObjectResponse(object_id=prepared.object_id)
    target = prepared.upload
    parsed = urlsplit(target.url)
    public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        try:
            with httpx.Client(
                follow_redirects=False, trust_env=False, timeout=timeout_seconds
            ) as client:
                response = client.put(
                    target.url, headers=target.headers, content=iter(body.read, b"")
                )
        except httpx.HTTPError:
            raise HttpTransportError(
                "PUT", public_url, "object storage upload transport failed"
            ) from None
        if not response.is_success:
            raise HttpApiError("object storage rejected upload", status_code=response.status_code)
        etag = response.headers.get("etag", "")
        if not etag:
            raise HttpResponseDecodeError("object storage upload did not return an ETag")
        try:
            completed = PutObjectResponse.model_validate(
                channel.post(
                    f"/gateway/objects/uploads/{prepared.object_id}/complete?{scope}",
                    {"claim_id": target.claim_id, "etag": etag},
                )
            )
        except ValidationError as exc:
            raise HttpResponseDecodeError(
                "object completion response contained invalid JSON"
            ) from exc
    except BaseException:
        # The durable claim keeps cleanup retryable if the API is unreachable.
        with suppress(Exception):
            channel.post(
                f"/gateway/objects/uploads/{prepared.object_id}/abort?{scope}",
                {"claim_id": target.claim_id},
            )
        raise
    body.finish(size)
    return completed


class _ProgressBytesReader:
    def __init__(
        self,
        data: bytes,
        *,
        progress: ProgressCallback | None,
        chunk_size: int,
    ) -> None:
        self._data = data
        self._progress = progress
        self._chunk_size = max(chunk_size, 1)
        self._offset = 0
        self._last_reported = -1

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        selected_size = (
            self._chunk_size if size is None or size < 0 else min(size, self._chunk_size)
        )
        end = min(self._offset + selected_size, len(self._data))
        chunk = self._data[self._offset : end]
        self._offset = end
        self._report(self._offset)
        return chunk

    def finish(self, size: int) -> None:
        self._report(size)

    def _report(self, completed: int) -> None:
        if self._progress is not None and completed != self._last_reported:
            self._progress(completed)
            self._last_reported = completed


class _ProgressFileReader:
    def __init__(
        self,
        stream: BinaryIO,
        *,
        progress: ProgressCallback | None,
        chunk_size: int,
    ) -> None:
        self._stream = stream
        self._progress = progress
        self._chunk_size = max(chunk_size, 1)
        self._completed = 0
        self._last_reported = -1

    def read(self, size: int = -1) -> bytes:
        selected_size = (
            self._chunk_size if size is None or size < 0 else min(size, self._chunk_size)
        )
        chunk = self._stream.read(selected_size)
        self._completed += len(chunk)
        self._report(self._completed)
        return chunk

    def finish(self, size: int) -> None:
        self._report(size)

    def _report(self, completed: int) -> None:
        if self._progress is not None and completed != self._last_reported:
            self._progress(completed)
            self._last_reported = completed


_ProgressReader: TypeAlias = _ProgressBytesReader | _ProgressFileReader


__all__ = [
    "DEFAULT_OBJECT_UPLOAD_TIMEOUT_SECONDS",
    "object_upload_timeout_seconds",
    "stream_object_bytes",
    "stream_object_file",
]
