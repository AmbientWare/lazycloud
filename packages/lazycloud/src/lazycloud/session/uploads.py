from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, TypeAlias
from urllib.parse import urlencode

from shared.http.errors import HttpResponseDecodeError
from shared.http.objects import ObjectMetadata, PutObjectRequest, PutObjectResponse
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
    """Upload one authenticated raw body and validate its committed object response."""
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
    headers = {
        "Content-Length": str(size),
        "Content-Type": upload.content_type,
        **_metadata_headers(upload.metadata),
    }
    query = urlencode(
        {
            "workspace": workspace,
            "bucket": upload.bucket,
            "name": upload.object_metadata.name,
            "hash": upload.hash,
            "size": upload.object_metadata.size,
            "overwrite": "true" if upload.overwrite else "false",
        }
    )
    response = channel.request_bytes(
        "POST",
        f"/gateway/objects/stream?{query}",
        data=iter(body.read, b""),
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    body.finish()
    try:
        return PutObjectResponse.model_validate_json(response)
    except ValueError as exc:
        raise HttpResponseDecodeError("object upload response contained invalid JSON") from exc


def _metadata_headers(metadata: dict[str, str]) -> dict[str, str]:
    return {f"x-object-meta-{key}": value for key, value in metadata.items()}


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

    def finish(self) -> None:
        self._report(len(self._data))

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

    def finish(self) -> None:
        self._report(self._completed)

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
