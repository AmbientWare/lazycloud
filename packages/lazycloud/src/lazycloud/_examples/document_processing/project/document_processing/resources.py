"""Shared LazyCloud resources for the document-processing example."""

from __future__ import annotations

from pathlib import Path

from lazycloud import App, Image, Volume

APP_NAME = "document_processing"
DATA_VOLUME_NAME = "document-processing-data"
DATA_ROOT = Path("/document-processing")
JOB_TOKEN_SECRET_NAME = "DOCUMENT_JOB_TOKEN_SECRET"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_OCR_CHARACTERS = 200_000

app = App(APP_NAME)
data_volume = Volume(DATA_VOLUME_NAME, str(DATA_ROOT))
runtime_image = (
    Image(python_version="3.12")
    .add_python_packages(["fastapi==0.139.2"])
    .add_commands(
        [
            (
                "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "--no-install-recommends poppler-utils tesseract-ocr && "
                "rm -rf /var/lib/apt/lists/*"
            )
        ]
    )
)
