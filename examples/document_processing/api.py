"""FastAPI upload, polling, result, and cleanup surface."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from examples.document_processing.models import (
    HealthResponse,
    JobStatusResponse,
    OcrResult,
    UploadAccepted,
)
from examples.document_processing.resources import DATA_ROOT, MAX_UPLOAD_BYTES
from examples.document_processing.security import (
    InvalidJobToken,
    JobClaims,
    get_job_token_secret,
    issue_job_token,
    verify_job_token,
)
from examples.document_processing.storage import (
    UploadValidationError,
    remove_document_files,
    result_path,
    upload_path,
    validate_upload,
    write_bounded_upload,
)
from examples.document_processing.worker import ocr_document
from lazycloud import Client, Task

STATIC_ROOT = Path(__file__).with_name("static")
TERMINAL_STATUSES = frozenset({"complete", "failed", "expired", "timeout", "cancelled"})
FAILED_STATUSES = frozenset({"failed", "expired", "timeout", "cancelled"})


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    (DATA_ROOT / "uploads").mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "results").mkdir(parents=True, exist_ok=True)
    yield


api = FastAPI(
    title="Document processing",
    description="Upload a bounded document and poll its durable OCR Task.",
    version="1.0.0",
    lifespan=lifespan,
)
api.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


@api.get("/", include_in_schema=False, response_class=FileResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@api.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@api.put(
    "/api/documents/{filename}",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(filename: str, request: Request) -> UploadAccepted:
    content_type = request.headers.get("content-type", "")
    try:
        _, suffix = validate_upload(filename, content_type)
    except UploadValidationError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    try:
        content_length = _content_length(request.headers.get("content-length"))
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if content_length is not None and content_length > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeds the {MAX_UPLOAD_BYTES}-byte limit",
        )
    try:
        secret = get_job_token_secret()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="job token signing is unavailable") from exc

    document_id = uuid.uuid4().hex
    destination = upload_path(DATA_ROOT, document_id, suffix)
    try:
        await write_bounded_upload(request.stream(), destination, max_bytes=MAX_UPLOAD_BYTES)
        task = await asyncio.to_thread(
            ocr_document.spawn,
            document_id,
            suffix,
        )
        token = issue_job_token(task.task_id, document_id, suffix, secret=secret)
    except UploadValidationError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return UploadAccepted(job_token=token)


@api.get("/api/jobs/status", response_model=JobStatusResponse)
async def job_status(x_job_token: str = Header(alias="X-Job-Token")) -> JobStatusResponse:
    task, claims = _task_from_token(x_job_token)
    view = await asyncio.to_thread(task.view)
    task_status = view.status.value
    if task_status in FAILED_STATUSES:
        upload_path(DATA_ROOT, claims.document_id, claims.suffix).unlink(missing_ok=True)
    return JobStatusResponse(
        status=task_status,
        ready=task_status == "complete",
        error="OCR processing failed" if task_status in FAILED_STATUSES else None,
    )


@api.get("/api/jobs/result", response_model=OcrResult)
async def job_result(x_job_token: str = Header(alias="X-Job-Token")) -> OcrResult:
    task, claims = _task_from_token(x_job_token)
    view = await asyncio.to_thread(task.view)
    if view.status.value != "complete":
        raise HTTPException(status_code=409, detail="OCR result is not ready")
    destination = result_path(DATA_ROOT, claims.document_id)
    if not destination.exists():
        # A consumed result is deleted by design, so its absence is an ordinary
        # outcome for a still-valid token rather than a server fault.
        raise HTTPException(status_code=404, detail="OCR result is no longer available")
    try:
        return OcrResult.model_validate_json(destination.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="OCR result is unavailable") from exc


@api.delete("/api/jobs", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(x_job_token: str = Header(alias="X-Job-Token")) -> Response:
    task, claims = _task_from_token(x_job_token)
    view = await asyncio.to_thread(task.view)
    if view.status.value not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="a running OCR job cannot be deleted")
    remove_document_files(DATA_ROOT, claims.document_id, claims.suffix)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _task_from_token(token: str) -> tuple[Task, JobClaims]:
    try:
        claims = verify_job_token(token, secret=get_job_token_secret())
    except (InvalidJobToken, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return Client().task_handle(claims.task_id), claims


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as exc:
        raise UploadValidationError("Content-Length must be an integer") from exc
    if length < 0:
        raise UploadValidationError("Content-Length must not be negative")
    return length
