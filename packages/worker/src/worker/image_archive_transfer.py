from __future__ import annotations

import hashlib
import os
import socket
import time
from base64 import b64encode
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from networking.internal_http import InternalHttpClient, InternalHttpError

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
    http: InternalHttpClient,
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
                http,
                url,
                path,
                headers=upload_headers,
                timeout_seconds=_remaining_timeout(deadline),
            )
            return
        except _ImageArchiveHttpError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
        except _TRANSIENT_TRANSFER_EXCEPTIONS as exc:
            if attempt == attempts - 1:
                # Name the failure and the destination host. Reporting only that
                # transient errors were exhausted leaves an operator unable to
                # tell an unreachable endpoint from a misconfigured one, and the
                # build fails identically either way. The signed URL still never
                # appears: only its host and port, which carry no capability.
                raise ImageArchiveTransferError(
                    "image archive upload failed after transient transfer errors "
                    f"to {_transfer_endpoint(url)}: {type(exc).__name__}: {exc}"
                ) from None
        except ImageArchiveTransferError:
            raise
        except Exception as exc:
            raise ImageArchiveTransferError(
                f"image archive upload failed: {type(exc).__name__}"
            ) from None
        _wait_before_retry(retry_delays_seconds[attempt], deadline)


def download_image_archive(
    http: InternalHttpClient,
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
                    http,
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
            except _TRANSIENT_TRANSFER_EXCEPTIONS as exc:
                if attempt == attempts - 1:
                    raise ImageArchiveTransferError(
                        "image archive download failed after transient transfer errors "
                        f"from {_transfer_endpoint(url)}: {type(exc).__name__}: {exc}"
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
    http: InternalHttpClient,
    url: str,
    path: Path,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> None:
    _require_http_url(url)
    with path.open("rb") as source:
        response = http.request(
            "PUT",
            url,
            headers=dict(headers),
            content=source,
            timeout_seconds=timeout_seconds,
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise _ImageArchiveHttpError("upload", response.status_code)


def _download_image_archive_once(
    http: InternalHttpClient,
    url: str,
    partial: Path,
    *,
    archive_size_bytes: int,
    archive_sha256: str,
    timeout_seconds: float,
) -> int:
    _require_http_url(url)
    with http.stream("GET", url, timeout_seconds=timeout_seconds) as response:
        if response.status_code < 200 or response.status_code >= 300:
            raise _ImageArchiveHttpError("download", response.status_code)
        response_length = response.headers.get("content-length")
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
            for chunk in response.iter_bytes(IMAGE_ARCHIVE_TRANSFER_CHUNK_SIZE_BYTES):
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


def _require_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ImageArchiveTransferError("image archive capability must use HTTP(S)")
    if parsed.hostname is None:
        raise ImageArchiveTransferError("image archive capability hostname is required")


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
        # Signed by the control plane, so the store rejects any body that is not
        # these exact bytes. A descriptor that omits or misdeclares it is refused
        # here, before anything leaves the worker.
        "x-amz-checksum-sha256": b64encode(bytes.fromhex(archive_sha256)).decode(),
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


def _transfer_endpoint(url: str) -> str:
    """Host and port of a transfer URL, never its capability path or query."""
    parsed = urlparse(url)
    return parsed.netloc or "an unknown host"


# InternalHttpError already wraps the transport failures the retry loop exists
# for, and carries no capability URL in its message.
_TRANSIENT_TRANSFER_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    InternalHttpError,
)
