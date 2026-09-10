from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from examples.document_processing.api import api as fastapi_app
from examples.document_processing.resources import (
    MAX_UPLOAD_BYTES,
)
from examples.document_processing.security import (
    InvalidJobToken,
    issue_job_token,
    verify_job_token,
)
from examples.document_processing.storage import (
    UploadValidationError,
    result_path,
    upload_path,
    validate_document_identity,
    validate_upload,
    write_bounded_upload,
)
from examples.document_processing.worker import process_document
from fastapi.testclient import TestClient

SECRET = b"s" * 48
DOCUMENT_ID = "a" * 32


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@pytest.mark.parametrize(
    ("filename", "content_type", "suffix"),
    [
        ("invoice.pdf", "application/pdf", ".pdf"),
        ("scan.jpg", "image/jpeg", ".jpg"),
        ("scan.jpeg", "image/jpeg; charset=binary", ".jpeg"),
        ("scan.png", "image/png", ".png"),
    ],
)
def test_upload_validation_accepts_matching_bounded_types(
    filename: str,
    content_type: str,
    suffix: str,
) -> None:
    assert validate_upload(filename, content_type) == (content_type.partition(";")[0], suffix)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("../secret.pdf", "application/pdf"),
        ("folder\\secret.pdf", "application/pdf"),
        ("script.exe", "application/pdf"),
        ("scan.png", "image/jpeg"),
        ("scan.svg", "image/svg+xml"),
        (".hidden.pdf", "application/pdf"),
    ],
)
def test_upload_validation_rejects_paths_and_mismatched_types(
    filename: str,
    content_type: str,
) -> None:
    with pytest.raises(UploadValidationError):
        validate_upload(filename, content_type)


def test_upload_route_distinguishes_type_header_and_size_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("examples.document_processing.api.DATA_ROOT", tmp_path)
    with TestClient(fastapi_app) as client:
        unsupported = client.put(
            "/api/documents/scan.png",
            content=b"not an image",
            headers={"Content-Type": "image/jpeg"},
        )
        malformed_length = client.put(
            "/api/documents/scan.png",
            content=b"not an image",
            headers={"Content-Type": "image/png", "Content-Length": "invalid"},
        )
        oversized = client.put(
            "/api/documents/scan.png",
            content=b"not an image",
            headers={
                "Content-Type": "image/png",
                "Content-Length": str(MAX_UPLOAD_BYTES + 1),
            },
        )

        assert unsupported.status_code == 415
        assert malformed_length.status_code == 400
        assert oversized.status_code == 413


def test_document_identity_cannot_escape_volume_root(tmp_path: Path) -> None:
    assert upload_path(tmp_path, DOCUMENT_ID, ".pdf").is_relative_to(tmp_path)
    assert result_path(tmp_path, DOCUMENT_ID).is_relative_to(tmp_path)

    with pytest.raises(ValueError):
        validate_document_identity("../escape", ".pdf")
    with pytest.raises(ValueError):
        validate_document_identity(DOCUMENT_ID, "/tmp/file")


def test_bounded_streaming_upload_is_atomic_and_removes_oversize_files(tmp_path: Path) -> None:
    destination = tmp_path / "uploads" / f"{DOCUMENT_ID}.pdf"
    size = asyncio.run(write_bounded_upload(_chunks(b"abc", b"def"), destination, max_bytes=6))

    assert size == 6
    assert destination.read_bytes() == b"abcdef"

    with pytest.raises(UploadValidationError):
        asyncio.run(write_bounded_upload(_chunks(b"1234", b"5678"), destination, max_bytes=7))
    assert not destination.exists()
    assert not list(destination.parent.glob("*.tmp"))


def test_job_tokens_bind_task_document_and_expiry() -> None:
    token = issue_job_token("task-123", DOCUMENT_ID, ".png", secret=SECRET, now=100)
    claims = verify_job_token(token, secret=SECRET, now=101)

    assert claims.task_id == "task-123"
    assert claims.document_id == DOCUMENT_ID
    assert claims.suffix == ".png"
    assert 100 < claims.expires_at <= 100 + 24 * 60 * 60

    encoded, signature = token.split(".")
    forged = f"{encoded[:-1]}A.{signature}"
    with pytest.raises(InvalidJobToken):
        verify_job_token(forged, secret=SECRET, now=101)
    with pytest.raises(InvalidJobToken):
        verify_job_token(token, secret=SECRET, now=claims.expires_at)
    with pytest.raises(InvalidJobToken):
        verify_job_token("not-base64.unsigned", secret=SECRET, now=101)


def test_worker_persists_ocr_output_and_removes_upload_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = upload_path(tmp_path, DOCUMENT_ID, ".png")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"image")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="Detected text\n", stderr="")

    monkeypatch.setattr("examples.document_processing.worker.subprocess.run", fake_run)
    value = process_document(DOCUMENT_ID, ".png", tmp_path)

    assert value == {
        "document_id": DOCUMENT_ID,
        "page_count": 1,
        "result_path": f"results/{DOCUMENT_ID}.json",
    }
    assert not source.exists()
    stored = json.loads(result_path(tmp_path, DOCUMENT_ID).read_text(encoding="utf-8"))
    assert stored == {
        "document_id": DOCUMENT_ID,
        "page_count": 1,
        "text": "Detected text",
    }

    source.write_bytes(b"image")

    def fail_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("examples.document_processing.worker.subprocess.run", fail_run)
    with pytest.raises(subprocess.CalledProcessError):
        process_document(DOCUMENT_ID, ".png", tmp_path)
    assert not source.exists()
    assert not result_path(tmp_path, DOCUMENT_ID).exists()
