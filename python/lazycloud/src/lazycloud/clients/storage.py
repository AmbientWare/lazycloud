"""Volumes, disks, artifacts, queues and maps through the public API.

Presigned URLs carry their own authorization, so the transfers here send them
with plain HTTP requests and never attach the bearer token.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
from shared.api import (
    AbortVolumeUploadRequest,
    Artifact,
    ArtifactPage,
    ArtifactSummary,
    ArtifactUpload,
    CompleteArtifactRequest,
    CompletedPart,
    CompleteVolumeUploadRequest,
    CreateArtifactRequest,
    CreateVolumeRequest,
    CreateVolumeUploadRequest,
    DiskPage,
    MapEntry,
    MapEntryWrite,
    MapInfo,
    MapKeyPage,
    MoveVolumeFileRequest,
    MultipartUpload,
    PresignArtifactRequest,
    PresignedUrl,
    PresignVolumeFileRequest,
    PutQueueMessagesRequest,
    QueueInfo,
    QueueMessageResult,
    RemovedVolumeFiles,
    SetMapEntryRequest,
    UploadPart,
    Volume,
    VolumeFile,
    VolumeFilePage,
    VolumePage,
)

from lazycloud.clients.api import ApiClient, ApiConnectionError, ApiError, _path

_TRANSFER_TIMEOUT = httpx.Timeout(30.0, read=600.0, write=600.0)
_TRANSFER_CHUNK_BYTES = 1024 * 1024
# Parts in flight at once during a multipart upload.
_PART_CONCURRENCY = 4

Query = dict[str, int | str]


@dataclass
class StorageClient:
    """The storage operations of one workspace."""

    api: ApiClient
    workspace: str

    # Volumes

    def list_volumes(self, *, cursor: str | None = None) -> VolumePage:
        return self.api._send(
            VolumePage, "GET", self._path("volumes"), params=_query(cursor=cursor)
        )

    def create_volume(self, name: str) -> Volume:
        return self.api._send(
            Volume, "POST", self._path("volumes"), body=CreateVolumeRequest(name=name)
        )

    def delete_volume(self, name: str) -> None:
        self.api._send(None, "DELETE", self._path("volumes", name))

    def list_volume_files(
        self, volume: str, path: str, *, cursor: str | None = None
    ) -> VolumeFilePage:
        return self.api._send(
            VolumeFilePage,
            "GET",
            self._path("volumes", volume, "files"),
            params=_query(path=path, cursor=cursor),
        )

    def remove_volume_files(self, volume: str, path: str) -> RemovedVolumeFiles:
        return self.api._send(
            RemovedVolumeFiles,
            "DELETE",
            self._path("volumes", volume, "files"),
            params={"path": path},
        )

    def stat_volume_file(self, volume: str, path: str) -> VolumeFile:
        return self.api._send(
            VolumeFile,
            "GET",
            self._path("volumes", volume, "files", "stat"),
            params={"path": path},
        )

    def move_volume_file(self, volume: str, source: str, destination: str) -> VolumeFile:
        return self.api._send(
            VolumeFile,
            "POST",
            self._path("volumes", volume, "files", "move"),
            body=MoveVolumeFileRequest.model_validate({"from": source, "to": destination}),
        )

    def presign_volume_file(self, volume: str, request: PresignVolumeFileRequest) -> PresignedUrl:
        return self.api._send(
            PresignedUrl, "POST", self._path("volumes", volume, "files", "url"), body=request
        )

    def create_volume_upload(
        self, volume: str, request: CreateVolumeUploadRequest
    ) -> MultipartUpload:
        return self.api._send(
            MultipartUpload, "POST", self._path("volumes", volume, "uploads"), body=request
        )

    def complete_volume_upload(
        self, volume: str, request: CompleteVolumeUploadRequest
    ) -> VolumeFile:
        return self.api._send(
            VolumeFile,
            "POST",
            self._path("volumes", volume, "uploads", "complete"),
            body=request,
        )

    def abort_volume_upload(self, volume: str, request: AbortVolumeUploadRequest) -> None:
        self.api._send(
            None, "POST", self._path("volumes", volume, "uploads", "abort"), body=request
        )

    # Disks

    def list_disks(self, *, cursor: str | None = None) -> DiskPage:
        return self.api._send(DiskPage, "GET", self._path("disks"), params=_query(cursor=cursor))

    def delete_disk(self, name: str) -> None:
        self.api._send(None, "DELETE", self._path("disks", name))

    # Artifacts

    def list_artifacts(
        self,
        *,
        task_id: UUID | None = None,
        app: str | None = None,
        search: str | None = None,
        content_type: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        cursor: str | None = None,
    ) -> ArtifactPage:
        return self.api._send(
            ArtifactPage,
            "GET",
            self._path("artifacts"),
            params=_query(
                task_id=str(task_id) if task_id is not None else None,
                app=app,
                search=search,
                content_type=content_type,
                created_after=created_after.isoformat() if created_after else None,
                created_before=created_before.isoformat() if created_before else None,
                cursor=cursor,
            ),
        )

    def create_artifact(self, request: CreateArtifactRequest) -> ArtifactUpload:
        return self.api._send(ArtifactUpload, "POST", self._path("artifacts"), body=request)

    def get_artifact_summary(self) -> ArtifactSummary:
        return self.api._send(ArtifactSummary, "GET", self._path("artifacts", "summary"))

    def get_artifact(self, artifact_id: UUID) -> Artifact:
        return self.api._send(Artifact, "GET", self._path("artifacts", str(artifact_id)))

    def delete_artifact(self, artifact_id: UUID) -> None:
        self.api._send(None, "DELETE", self._path("artifacts", str(artifact_id)))

    def complete_artifact(self, artifact_id: UUID, request: CompleteArtifactRequest) -> Artifact:
        return self.api._send(
            Artifact, "POST", self._path("artifacts", str(artifact_id), "complete"), body=request
        )

    def presign_artifact(self, artifact_id: UUID, request: PresignArtifactRequest) -> PresignedUrl:
        return self.api._send(
            PresignedUrl, "POST", self._path("artifacts", str(artifact_id), "url"), body=request
        )

    # Queues

    def get_queue(self, name: str) -> QueueInfo:
        return self.api._send(QueueInfo, "GET", self._path("queues", name))

    def delete_queue(self, name: str) -> None:
        self.api._send(None, "DELETE", self._path("queues", name))

    def put_queue_messages(self, name: str, messages: Sequence[bytes]) -> None:
        request = PutQueueMessagesRequest(messages=[base64.b64encode(item) for item in messages])
        self.api._send(None, "POST", self._path("queues", name, "messages"), body=request)

    def pop_queue_message(self, name: str, *, wait_seconds: int = 0) -> QueueMessageResult:
        return self.api._send(
            QueueMessageResult,
            "POST",
            self._path("queues", name, "pop"),
            params={"wait_seconds": wait_seconds} if wait_seconds else None,
            read_timeout=wait_seconds + self.api.timeout_seconds if wait_seconds else None,
        )

    def peek_queue_message(self, name: str) -> QueueMessageResult:
        return self.api._send(QueueMessageResult, "GET", self._path("queues", name, "head"))

    # Maps

    def get_map(self, name: str) -> MapInfo:
        return self.api._send(MapInfo, "GET", self._path("maps", name))

    def delete_map(self, name: str) -> None:
        self.api._send(None, "DELETE", self._path("maps", name))

    def list_map_keys(
        self, name: str, *, prefix: str | None = None, cursor: str | None = None
    ) -> MapKeyPage:
        return self.api._send(
            MapKeyPage,
            "GET",
            self._path("maps", name, "keys"),
            params=_query(prefix=prefix, cursor=cursor),
        )

    def get_map_entry(self, name: str, key: str) -> MapEntry:
        return self.api._send(MapEntry, "GET", self._path("maps", name, "entries", key))

    def set_map_entry(self, name: str, key: str, request: SetMapEntryRequest) -> MapEntryWrite:
        return self.api._send(
            MapEntryWrite, "PUT", self._path("maps", name, "entries", key), body=request
        )

    def delete_map_entry(self, name: str, key: str, *, if_revision: str | None = None) -> None:
        self.api._send(
            None,
            "DELETE",
            self._path("maps", name, "entries", key),
            params=_query(if_revision=if_revision),
        )

    def _path(self, *segments: str) -> str:
        return _path("v1", "workspaces", self.workspace, *segments)


def put_presigned(url: str, content: bytes) -> str:
    """PUT bytes to a presigned URL and return the stored object's ETag."""
    with httpx.Client(timeout=_TRANSFER_TIMEOUT) as http:
        return _put(http, url, content, len(content))


def get_presigned(url: str) -> bytes:
    """The bytes at a download URL, following its redirect to the store."""
    try:
        response = httpx.get(url, timeout=_TRANSFER_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ApiConnectionError("GET", "presigned download", _reason(exc)) from exc
    _check_transfer(response, "download")
    return response.content


def download_presigned(url: str, destination: Path) -> None:
    """Stream a download URL into a file, replacing it."""
    try:
        with httpx.stream("GET", url, timeout=_TRANSFER_TIMEOUT, follow_redirects=True) as response:
            if response.status_code >= 300:
                response.read()
                _check_transfer(response, "download")
            with destination.open("wb") as target:
                for chunk in response.iter_bytes(_TRANSFER_CHUNK_BYTES):
                    target.write(chunk)
    except httpx.HTTPError as exc:
        raise ApiConnectionError("GET", "presigned download", _reason(exc)) from exc


def upload_file_parts(source: Path, parts: Sequence[UploadPart]) -> list[CompletedPart]:
    """Send each part's byte range of a file to its URL, a few at a time.

    The returned parts are in part-number order, as completion requires.
    """
    with (
        httpx.Client(timeout=_TRANSFER_TIMEOUT) as http,
        ThreadPoolExecutor(max_workers=_PART_CONCURRENCY) as pool,
    ):

        def send(part: UploadPart) -> str:
            content = _file_range(source, part.offset, part.size_bytes)
            return _put(http, part.url, content, part.size_bytes)

        etags = pool.map(send, parts)
        completed = [
            CompletedPart(number=part.number, etag=etag)
            for part, etag in zip(parts, etags, strict=True)
        ]
    return sorted(completed, key=lambda part: part.number)


def _put(http: httpx.Client, url: str, content: bytes | Iterator[bytes], size: int) -> str:
    try:
        response = http.put(url, content=content, headers={"Content-Length": str(size)})
    except httpx.HTTPError as exc:
        raise ApiConnectionError("PUT", "presigned upload", _reason(exc)) from exc
    _check_transfer(response, "upload")
    return response.headers.get("ETag", "")


def _file_range(source: Path, offset: int, size: int) -> Iterator[bytes]:
    with source.open("rb") as file:
        file.seek(offset)
        remaining = size
        while remaining > 0:
            chunk = file.read(min(_TRANSFER_CHUNK_BYTES, remaining))
            if not chunk:
                msg = f"{source} ended before byte {offset + size}"
                raise ApiConnectionError("PUT", "presigned upload", msg)
            remaining -= len(chunk)
            yield chunk


def _check_transfer(response: httpx.Response, action: str) -> None:
    if response.status_code >= 300:
        raise ApiError(
            status_code=response.status_code,
            code=None,
            message=f"the object store refused the {action}: {response.text[:500]}",
        )


def _reason(exc: httpx.HTTPError) -> str:
    return str(exc) or type(exc).__name__


def _query(**values: str | None) -> Query | None:
    query: Query = {name: value for name, value in values.items() if value is not None}
    return query or None


__all__ = [
    "StorageClient",
    "download_presigned",
    "get_presigned",
    "put_presigned",
    "upload_file_parts",
]
