from __future__ import annotations

import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from lazycloud.abstractions.volume import (
    CompletedPart,
    MultipartUploadPlan,
    PresignedUrl,
    PresignedUrlMethod,
    Volume,
)
from lazycloud.exceptions import ObjectUploadError

ProgressCallback = Callable[[int, int], None]


@runtime_checkable
class ResponseHeaders(Protocol):
    def get(self, name: str, default: str = "") -> str: ...


@runtime_checkable
class UploadResponse(Protocol):
    status: int
    headers: ResponseHeaders

    def __enter__(self) -> UploadResponse: ...

    def __exit__(self, *args: object) -> None: ...


@runtime_checkable
class DownloadResponse(Protocol):
    def __enter__(self) -> DownloadResponse: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


def upload_file(
    volume: Volume,
    source: str | Path,
    destination: str | Path | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> str:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        return volume.put(source_path, destination)
    target = destination or source_path.name
    plan = volume.create_multipart_upload(target, file_size=source_path.stat().st_size)
    completed = upload_parts(source_path, plan, progress=progress)
    volume.complete_multipart_upload(plan.upload_id, target, completed)
    return str(target)


def upload_parts(
    source: str | Path,
    plan: MultipartUploadPlan,
    *,
    progress: ProgressCallback | None = None,
) -> list[CompletedPart]:
    source_path = Path(source).expanduser().resolve()
    completed: list[CompletedPart] = []
    with source_path.open("rb") as handle:
        for part in plan.parts:
            handle.seek(part.start)
            data = handle.read(part.end - part.start)
            request = urllib.request.Request(part.url, data=data, method="PUT")
            with _upload_response(request) as response:
                if response.status >= 400:
                    msg = f"part upload failed: part={part.number} status={response.status}"
                    raise ObjectUploadError(msg)
                etag = response.headers.get("ETag", "").strip('"')
            if progress is not None:
                progress(plan.file_size, len(data))
            completed.append(CompletedPart(number=part.number, etag=etag))
    return completed


def download_bytes(url: str | PresignedUrl, *, timeout_seconds: float = 30.0) -> bytes:
    selected_url = url.url if isinstance(url, PresignedUrl) else url
    with _download_response(selected_url, timeout_seconds=timeout_seconds) as response:
        return response.read()


def _upload_response(request: urllib.request.Request) -> UploadResponse:
    response: object = urllib.request.urlopen(request)
    if not isinstance(response, UploadResponse):
        raise ObjectUploadError("multipart upload returned an invalid HTTP response")
    return response


def _download_response(url: str, *, timeout_seconds: float) -> DownloadResponse:
    response: object = urllib.request.urlopen(url, timeout=timeout_seconds)
    if not isinstance(response, DownloadResponse):
        raise ObjectUploadError("object download returned an invalid HTTP response")
    return response


__all__ = [
    "PresignedUrlMethod",
    "ProgressCallback",
    "download_bytes",
    "upload_file",
    "upload_parts",
]
