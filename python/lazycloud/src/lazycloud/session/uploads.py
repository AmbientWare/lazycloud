from __future__ import annotations

import hashlib
import io
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryFile
from typing import BinaryIO
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError
from shared.http.objects import (
    BeginObjectUploadResponse,
    CompletedObjectUploadPart,
    CompleteObjectUploadRequest,
    ObjectMetadata,
    ObjectUploadPartRequest,
    ObjectUploadPartResponse,
    PutObjectRequest,
    PutObjectResponse,
    object_upload_part_count,
    object_upload_part_size,
)
from shared.http_transport import HttpChannel
from shared.transport_retry import TRANSIENT_TRANSPORT_ERRORS, call_with_transient_retry

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
    with io.BytesIO(data) as source:
        return _stream_object(
            channel=channel,
            workspace=workspace,
            source=source,
            size=len(data),
            name=name,
            object_hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=content_type,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            progress=progress,
            chunk_size=chunk_size,
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
    with Path(source).expanduser().resolve().open("rb") as stream:
        return _stream_object(
            channel=channel,
            workspace=workspace,
            source=stream,
            size=size,
            name=name,
            object_hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=content_type,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
            progress=progress,
            chunk_size=chunk_size,
        )


def _stream_object(
    *,
    channel: HttpChannel,
    workspace: str,
    source: BinaryIO,
    size: int,
    name: str,
    object_hash: str,
    bucket: str,
    overwrite: bool,
    content_type: str,
    metadata: dict[str, str] | None,
    timeout_seconds: float,
    progress: ProgressCallback | None,
    chunk_size: int,
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
            channel.post(f"/gateway/objects/uploads?{scope}", upload.model_dump(mode="json"))
        )
    except ValidationError as exc:
        raise HttpResponseDecodeError("object upload response contained invalid JSON") from exc
    if prepared.upload is None:
        if progress is not None:
            progress(size)
        return PutObjectResponse(object_id=prepared.object_id)
    claim_id = prepared.upload.claim_id
    path = f"/gateway/objects/uploads/{prepared.object_id}"
    part_size = object_upload_part_size(size)
    digest = hashlib.sha256()
    completed: list[CompletedObjectUploadPart] = []
    chunk_size = max(1, min(chunk_size, 1024 * 1024))
    reported = -1

    def report(value: int) -> None:
        nonlocal reported
        if progress is not None and value > reported:
            progress(value)
            reported = value

    try:
        with (
            httpx.Client(
                follow_redirects=False, trust_env=False, timeout=timeout_seconds
            ) as client,
            TemporaryFile() as snapshot,
        ):
            for number in range(1, object_upload_part_count(size) + 1):
                offset = (number - 1) * part_size
                remaining = min(part_size, size - offset)
                snapshot.seek(0)
                snapshot.truncate()
                part_digest = hashlib.sha256()
                while remaining:
                    chunk = source.read(min(chunk_size, remaining))
                    if not chunk:
                        raise ValueError("upload source changed size while being read")
                    snapshot.write(chunk)
                    digest.update(chunk)
                    part_digest.update(chunk)
                    remaining -= len(chunk)
                request = ObjectUploadPartRequest(
                    claim_id=claim_id, part_number=number, sha256=part_digest.hexdigest()
                )

                completed.append(
                    CompletedObjectUploadPart(
                        part_number=number,
                        etag=_upload_part(
                            channel=channel,
                            client=client,
                            path=path,
                            scope=scope,
                            request=request,
                            snapshot=snapshot,
                            offset=offset,
                            chunk_size=chunk_size,
                            report=report,
                        ),
                    )
                )
            if source.read(1) or digest.hexdigest() != object_hash:
                raise ValueError("upload source changed since its checksum was calculated")
        request_complete = CompleteObjectUploadRequest(claim_id=claim_id, parts=completed)

        def complete() -> PutObjectResponse:
            try:
                return PutObjectResponse.model_validate(
                    channel.post(
                        f"{path}/complete?{scope}", request_complete.model_dump(mode="json")
                    )
                )
            except ValidationError as exc:
                raise HttpResponseDecodeError(
                    "object completion response contained invalid JSON"
                ) from exc

        result = call_with_transient_retry(complete)
    except BaseException:
        # The durable claim keeps cleanup retryable if the API is unreachable.
        with suppress(Exception):
            channel.post(f"{path}/abort?{scope}", {"claim_id": claim_id})
        raise
    report(size)
    return result


def _upload_part(
    *,
    channel: HttpChannel,
    client: httpx.Client,
    path: str,
    scope: str,
    request: ObjectUploadPartRequest,
    snapshot: BinaryIO,
    offset: int,
    chunk_size: int,
    report: ProgressCallback,
) -> str:
    def send() -> str:
        try:
            target = ObjectUploadPartResponse.model_validate(
                channel.post(f"{path}/parts?{scope}", request.model_dump(mode="json"))
            )
        except ValidationError as exc:
            raise HttpResponseDecodeError(
                "object upload part response contained invalid JSON"
            ) from exc
        snapshot.seek(0)

        def chunks() -> Iterator[bytes]:
            sent = offset
            while chunk := snapshot.read(chunk_size):
                yield chunk
                sent += len(chunk)
                report(sent)

        parsed = urlsplit(target.url)
        public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        try:
            response = client.put(target.url, headers=target.headers, content=chunks())
        except httpx.HTTPError:
            raise HttpTransportError(
                "PUT", public_url, "object storage upload transport failed"
            ) from None
        if not response.is_success:
            raise HttpApiError("object storage rejected upload", status_code=response.status_code)
        etag = response.headers.get("etag", "")
        if not etag:
            raise HttpResponseDecodeError("object storage upload did not return an ETag")
        return etag

    for attempt in range(5):
        try:
            return send()
        except HttpApiError as exc:
            if exc.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 4:
                raise
        except TRANSIENT_TRANSPORT_ERRORS:
            if attempt == 4:
                raise
        time.sleep(min(2**attempt, 8))
    raise RuntimeError("object upload retry loop exhausted")


__all__ = [
    "DEFAULT_OBJECT_UPLOAD_TIMEOUT_SECONDS",
    "object_upload_timeout_seconds",
    "stream_object_bytes",
    "stream_object_file",
]
