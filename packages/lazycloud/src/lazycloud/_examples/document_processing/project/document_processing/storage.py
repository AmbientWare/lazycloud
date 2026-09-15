"""Validated, atomic file operations for uploaded documents and OCR results."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterable
from pathlib import Path

SUPPORTED_UPLOADS: dict[str, frozenset[str]] = {
    "application/pdf": frozenset({".pdf"}),
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
}
DOCUMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")


class UploadValidationError(ValueError):
    """Raised when an upload cannot be safely stored."""


def validate_upload(filename: str, content_type: str) -> tuple[str, str]:
    normalized_type = content_type.partition(";")[0].strip().lower()
    if normalized_type not in SUPPORTED_UPLOADS:
        raise UploadValidationError("supported content types are PDF, JPEG, and PNG")
    if not FILENAME_PATTERN.fullmatch(filename):
        raise UploadValidationError("filename contains unsupported characters")
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise UploadValidationError("filename must not contain a path")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_UPLOADS[normalized_type]:
        raise UploadValidationError("filename extension does not match the content type")
    return normalized_type, suffix


def validate_document_identity(document_id: str, suffix: str) -> tuple[str, str]:
    if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise ValueError("document id must be 32 lowercase hexadecimal characters")
    normalized_suffix = suffix.lower()
    supported_suffixes = {item for values in SUPPORTED_UPLOADS.values() for item in values}
    if normalized_suffix not in supported_suffixes:
        raise ValueError("unsupported document suffix")
    return document_id, normalized_suffix


def upload_path(root: Path, document_id: str, suffix: str) -> Path:
    safe_id, safe_suffix = validate_document_identity(document_id, suffix)
    return root / "uploads" / f"{safe_id}{safe_suffix}"


def result_path(root: Path, document_id: str) -> Path:
    safe_id, _ = validate_document_identity(document_id, ".pdf")
    return root / "results" / f"{safe_id}.json"


async def write_bounded_upload(
    chunks: AsyncIterable[bytes],
    destination: Path,
    *,
    max_bytes: int,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    size = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise UploadValidationError(f"upload exceeds the {max_bytes}-byte limit")
                handle.write(chunk)
            if size == 0:
                raise UploadValidationError("upload body is empty")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return size


def write_json_atomic(destination: Path, value: dict[str, object]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def remove_document_files(root: Path, document_id: str, suffix: str) -> None:
    upload_path(root, document_id, suffix).unlink(missing_ok=True)
    result_path(root, document_id).unlink(missing_ok=True)
