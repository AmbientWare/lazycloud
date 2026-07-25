"""Durable OCR task queue worker backed by Tesseract and Poppler."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from examples.document_processing.models import OcrTaskResult
from examples.document_processing.resources import (
    DATA_ROOT,
    MAX_OCR_CHARACTERS,
    MAX_PDF_PAGES,
    app,
    data_volume,
    runtime_image,
)
from examples.document_processing.storage import (
    result_path,
    upload_path,
    validate_document_identity,
    write_json_atomic,
)

OCR_COMMAND_TIMEOUT_SECONDS = 180


def process_document(document_id: str, suffix: str, root: Path) -> OcrTaskResult:
    safe_id, safe_suffix = validate_document_identity(document_id, suffix)
    source = upload_path(root, safe_id, safe_suffix)
    destination = result_path(root, safe_id)
    destination.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="document-ocr-") as temporary:
            workdir = Path(temporary)
            pages = _render_pages(source, safe_suffix, workdir)
            text_parts = [_read_page(page) for page in pages]
            text = "\n\n".join(part.strip() for part in text_parts if part.strip())
            if len(text) > MAX_OCR_CHARACTERS:
                raise RuntimeError("OCR result exceeds the configured text limit")
            value: dict[str, object] = {
                "document_id": safe_id,
                "page_count": len(pages),
                "text": text,
            }
            write_json_atomic(destination, value)
    finally:
        source.unlink(missing_ok=True)
    print(json.dumps({"event": "ocr-complete", "document_id": safe_id}), flush=True)
    return {
        "document_id": safe_id,
        "page_count": len(pages),
        "result_path": f"results/{safe_id}.json",
    }


def _render_pages(source: Path, suffix: str, workdir: Path) -> list[Path]:
    if suffix != ".pdf":
        return [source]
    page_count = _pdf_page_count(source)
    if page_count > MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
    prefix = workdir / "page"
    subprocess.run(
        ["pdftoppm", "-png", "-r", "200", str(source), str(prefix)],
        check=True,
        capture_output=True,
        timeout=OCR_COMMAND_TIMEOUT_SECONDS,
    )
    pages = sorted(workdir.glob("page-*.png"))
    if not pages:
        raise RuntimeError("Poppler did not produce any document pages")
    return pages


def _pdf_page_count(source: Path) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(source)],
        check=True,
        capture_output=True,
        text=True,
        timeout=OCR_COMMAND_TIMEOUT_SECONDS,
    )
    for line in completed.stdout.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() == "pages":
            count = int(value.strip())
            if count < 1:
                break
            return count
    raise RuntimeError("could not determine the PDF page count")


def _read_page(page: Path) -> str:
    completed = subprocess.run(
        ["tesseract", str(page), "stdout", "--dpi", "200"],
        check=True,
        capture_output=True,
        text=True,
        timeout=OCR_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout


@app.task_queue(
    name="ocr-worker",
    image=runtime_image,
    cpu=2.0,
    memory="4Gi",
    timeout=600,
    retries=0,
    workers=1,
    keep_warm_seconds=0,
    max_pending_tasks=50,
    volumes=[data_volume],
    authorized=True,
)
def ocr_document(document_id: str, suffix: str) -> OcrTaskResult:
    return process_document(document_id, suffix, DATA_ROOT)
