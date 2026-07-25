from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

from lazycloud import App, CloudBucket, CloudBucketConfig, Image

APP_NAME = os.getenv("LAZYCLOUD_E2E_APP", f"cloud_bucket_{secrets.token_hex(6)}")
BUCKET_NAME = os.environ["LAZYCLOUD_E2E_BUCKET"]
BUCKET_PREFIX = os.environ["LAZYCLOUD_E2E_BUCKET_PREFIX"]
ACCESS_KEY_SECRET = os.environ["LAZYCLOUD_E2E_ACCESS_SECRET"]
SECRET_KEY_SECRET = os.environ["LAZYCLOUD_E2E_SECRET_SECRET"]
ROOT = Path("/mnt/e2e-cloud-bucket")

app = App(APP_NAME)
bucket = CloudBucket(
    BUCKET_NAME,
    str(ROOT),
    CloudBucketConfig(
        prefix=BUCKET_PREFIX,
        endpoint=os.getenv("LAZYCLOUD_E2E_BUCKET_INTERNAL_ENDPOINT", "http://object-store:9000"),
        region=os.getenv("LAZYCLOUD_E2E_BUCKET_REGION", "us-east-1"),
        access_key=ACCESS_KEY_SECRET,
        secret_key=SECRET_KEY_SECRET,
        force_path_style=True,
    ),
)


@app.function(
    name="cloud-bucket-probe",
    image=Image(python_version="3.12"),
    volumes=[bucket],
    secrets=[ACCESS_KEY_SECRET, SECRET_KEY_SECRET],
    cpu=0.25,
    memory="128Mi",
)
def cloud_bucket_probe(
    operation: Literal["read", "write"],
    relative_path: str,
    value: str = "",
) -> str:
    target = ROOT / relative_path
    if operation == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    return target.read_text(encoding="utf-8")
