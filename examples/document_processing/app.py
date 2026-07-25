"""Complete document-processing application declaration.

Deploy both the public ASGI surface and the private OCR queue with::

    lazycloud deploy examples.document_processing.app:app
"""

from __future__ import annotations

from examples.document_processing.api import api
from examples.document_processing.resources import (
    JOB_TOKEN_SECRET_NAME,
    app,
    data_volume,
    runtime_image,
)
from examples.document_processing.worker import ocr_document

web = app.asgi(
    name="document-api",
    image=runtime_image,
    route="/",
    cpu=1.0,
    memory="1Gi",
    timeout_seconds=300,
    workers=1,
    concurrent_requests=8,
    keep_warm_seconds=60,
    max_pending_tasks=50,
    authorized=False,
    secrets=[JOB_TOKEN_SECRET_NAME],
    volumes=[data_volume],
)(api)

__all__ = ["app", "ocr_document", "web"]
