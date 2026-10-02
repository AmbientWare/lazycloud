"""Upload a PDF or image in a browser and extract its text with background OCR.

Provision the signing secret using the guide at
https://docs.lazycloud.dev/examples/document-processing-asgi, then deploy both workloads:

    uv run lazycloud deploy document_processing.app:app

Open the printed document-api URL. Uploads are public in this example; use test
documents and delete the deployment when finished. Both workloads mount the
volume declared in resources.py; LazyCloud creates it on first use.
"""

from .api import api
from .resources import (
    JOB_TOKEN_SECRET_NAME,
    app,
    data_volume,
    runtime_image,
)
from .worker import ocr_document

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
