from __future__ import annotations

import hashlib
import http.client
import os
import socket
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

IMAGE_ARCHIVE_TRANSFER_CHUNK_SIZE_BYTES = 1024 * 1024
IMAGE_ARCHIVE_TRANSFER_RETRY_DELAYS_SECONDS = (0.5, 2.0, 5.0)


class ImageArchiveTransferError(RuntimeError):
    """Terminal image archive transfer failure safe to surface without a capability URL."""


class ImageArchiveIntegrityError(ImageArchiveTransferError):
    """The transferred bytes do not match their durable archive identity."""


class _ImageArchiveHttpError(ImageArchiveTransferError):
    def __init__(self, operation: str, status: int) -> None:
        super().__init__(f"image archive {operation} returned HTTP {status}")
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status in {408, 425, 429} or self.status >= 500


def image_archive_file_identity(path: Path) -> tuple[int, str]:
    size_bytes = 0
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(IMAGE_ARCHIVE_TRANSFER_CHUNK_SIZE_BYTES):
            size_bytes += len(chunk)
            digest.update(chunk)
    return size_bytes, digest.hexdigest()


def upload_image_archive(
    url: str,
    path: Path,
    *,
    headers: Mapping[str, str],
    archive_size_bytes: int,
    archive_sha256: str,
    content_type: str,
    timeout_seconds: float,
    retry_delays_seconds: Sequence[float] = IMAGE_ARCHIVE_TRANSFER_RETRY_DELAYS_SECONDS,
) -> None:
    _validate_archive_identity(archive_size_bytes, archive_sha256)
    upload_headers = _validated_upload_headers(
        headers,
        archive_size_bytes=archive_size_bytes,
        archive_sha256=archive_sha256,
        content_type=content_type,
    )
    _require_matching_file_identity(
        path,
        archive_size_bytes=archive_size_bytes,
        archive_sha256=archive_sha256,
    )
    deadline = time.monotonic() + timeout_seconds
    attempts = len(retry_delays_seconds) + 1
    for attempt in range(attempts):
        try:
            _require_matching_file_identity(
                path,
                archive_size_bytes=archive_size_bytes,
                archive_sha256=archive_sha256,
            )
            _upload_image_archive_once(
                url,
                path,
                headers=upload_headers,
                timeout_seconds=_remaining_timeout(deadline),
            )
            return
        except _ImageArchiveHttpError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
        except _TRANSIENT_TRANSFER_EXCEPTIONS:
            if attempt == attempts - 1:
                raise ImageArchiveTransferError(
                    "image archive upload failed after transient transfer errors"
                ) from None
        except ImageArchiveTransferError:
            raise
        except Exception as exc:
            raise ImageArchiveTransferError(
                f"image archive upload failed: {type(exc).__name__}"
            ) from None
        _wait_before_retry(retry_delays_seconds[attempt], deadline)


def download_image_archive(
    url: str,
    target: Path,
    *,
    archive_size_bytes: int,
    archive_sha256: str,
    timeout_seconds: float,
    retry_delays_seconds: Sequence[float] = IMAGE_ARCHIVE_TRANSFER_RETRY_DELAYS_SECONDS,
) -> int:
    _validate_archive_identity(archive_size_bytes, archive_sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{uuid4().hex}.partial")
    deadline = time.monotonic() + timeout_seconds
    attempts = len(retry_delays_seconds) + 1
    try:
        for attempt in range(attempts):
            try:
                bytes_written = _download_image_archive_once(
                    url,
                    partial,
                    archive_size_bytes=archive_size_bytes,
                    archive_sha256=archive_sha256,
                    timeout_seconds=_remaining_timeout(deadline),
                )
                os.replace(partial, target)
                _fsync_directory(target.parent)
                return bytes_written
            except _ImageArchiveHttpError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
            except _TRANSIENT_TRANSFER_EXCEPTIONS:
                if attempt == attempts - 1:
                    raise ImageArchiveTransferError(
                        "image archive download failed after transient transfer errors"
                    ) from None
            except ImageArchiveTransferError:
                raise
            except Exception as exc:
                raise ImageArchiveTransferError(
                    f"image archive download failed: {type(exc).__name__}"
                ) from None
            partial.unlink(missing_ok=True)
            _wait_before_retry(retry_delays_seconds[attempt], deadline)
    finally:
        partial.unlink(missing_ok=True)
    raise ImageArchiveTransferError("image archive download did not complete")


def _upload_image_archive_once(
    url: str,
    path: Path,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> None:
    connection, request_target = _connection(url, timeout_seconds=timeout_seconds)
    try:
        with path.open("rb") as source:
            connection.request("PUT", request_target, body=source, headers=dict(headers))
            response = connection.getresponse()
            response.read(4096)
            if response.status < 200 or response.status >= 300:
                raise _ImageArchiveHttpError("upload", response.status)
    finally:
        connection.close()


def _download_image_archive_once(
    url: str,
    partial: Path,
    *,
    archive_size_bytes: int,
    archive_sha256: str,
    timeout_seconds: float,
) -> int:
    connection, request_target = _connection(url, timeout_seconds=timeout_seconds)
    try:
        connection.request("GET", request_target)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            response.read(4096)
            raise _ImageArchiveHttpError("download", response.status)
        response_length = response.getheader("content-length")
        if response_length is not None:
            try:
                content_length = int(response_length)
            except ValueError as exc:
                raise ImageArchiveIntegrityError(
                    "image archive download returned an invalid content length"
                ) from exc
            if content_length != archive_size_bytes:
                raise ImageArchiveIntegrityError(
                    "image archive download content length does not match its descriptor"
                )

        digest = hashlib.sha256()
        bytes_written = 0
        with partial.open("xb") as output:
            while chunk := response.read(IMAGE_ARCHIVE_TRANSFER_CHUNK_SIZE_BYTES):
                bytes_written += len(chunk)
                if bytes_written > archive_size_bytes:
                    raise ImageArchiveIntegrityError(
                        "image archive download exceeded its expected size"
                    )
                digest.update(chunk)
                output.write(chunk)
            if bytes_written != archive_size_bytes:
                raise ImageArchiveIntegrityError(
                    "image archive download ended before its expected size"
                )
            if digest.hexdigest() != archive_sha256:
                raise ImageArchiveIntegrityError(
                    "image archive download digest does not match its descriptor"
                )
            output.flush()
            os.fsync(output.fileno())
        return bytes_written
    finally:
        connection.close()


def _connection(
    url: str,
    *,
    timeout_seconds: float,
) -> tuple[http.client.HTTPConnection, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ImageArchiveTransferError("image archive capability must use HTTP(S)")
    if parsed.hostname is None:
        raise ImageArchiveTransferError("image archive capability hostname is required")
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout_seconds)
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"
    return connection, request_target


def _validated_upload_headers(
    headers: Mapping[str, str],
    *,
    archive_size_bytes: int,
    archive_sha256: str,
    content_type: str,
) -> dict[str, str]:
    normalized: dict[str, tuple[str, str]] = {}
    for name, value in headers.items():
        if name != name.strip() or not name or not value or "\r" in value or "\n" in value:
            raise ImageArchiveTransferError("image archive upload descriptor has an invalid header")
        lower_name = name.lower()
        if lower_name in normalized:
            raise ImageArchiveTransferError(
                "image archive upload descriptor has duplicate signed headers"
            )
        normalized[lower_name] = (name, value)

    required = {
        "content-length": str(archive_size_bytes),
        "content-type": content_type,
        "x-amz-meta-artifact-sha256": archive_sha256,
    }
    for name, expected in required.items():
        actual = normalized.get(name)
        if actual is None or actual[1] != expected:
            raise ImageArchiveTransferError(
                f"image archive upload descriptor has invalid signed {name}"
            )
    return {original: value for original, value in normalized.values()}


def _validate_archive_identity(archive_size_bytes: int, archive_sha256: str) -> None:
    if archive_size_bytes <= 0:
        raise ImageArchiveIntegrityError("image archive size must be positive")
    if len(archive_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in archive_sha256
    ):
        raise ImageArchiveIntegrityError("image archive SHA-256 is invalid")


def _require_matching_file_identity(
    path: Path,
    *,
    archive_size_bytes: int,
    archive_sha256: str,
) -> None:
    actual_size_bytes, actual_sha256 = image_archive_file_identity(path)
    if actual_size_bytes != archive_size_bytes or actual_sha256 != archive_sha256:
        raise ImageArchiveIntegrityError(
            "image archive file changed after its upload descriptor was requested"
        )


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ImageArchiveTransferError("image archive transfer deadline expired")
    return remaining


def _wait_before_retry(delay_seconds: float, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or delay_seconds >= remaining:
        raise ImageArchiveTransferError("image archive transfer deadline expired")
    time.sleep(delay_seconds)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_TRANSIENT_TRANSFER_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    http.client.HTTPException,
)
