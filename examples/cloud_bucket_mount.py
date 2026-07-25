from __future__ import annotations

import time
from pathlib import Path
from typing import TypedDict

from lazycloud import App, CloudBucket, CloudBucketConfig

MOUNT_PATH = Path("/cloud-bucket-smoke")

cloud_bucket_app = App("cloud_bucket_smoke")
cloud_bucket = CloudBucket(
    "lazycloud-data",
    str(MOUNT_PATH),
    CloudBucketConfig(
        access_key="CLOUD_BUCKET_ACCESS_KEY",
        secret_key="CLOUD_BUCKET_SECRET_KEY",
        endpoint="http://object-store:9000",
        region="us-east-1",
        prefix="e2e/cloud-bucket",
        force_path_style=True,
    ),
)


class CloudBucketSmokeResult(TypedDict):
    key: str
    value: str


@cloud_bucket_app.function(
    name="cloud-bucket-write-read",
    volumes=[cloud_bucket],
    timeout_seconds=120,
)
def write_and_read(key: str, value: str, hold_seconds: float = 0) -> CloudBucketSmokeResult:
    target = MOUNT_PATH / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    if hold_seconds > 0:
        time.sleep(hold_seconds)
    return {"key": key, "value": target.read_text(encoding="utf-8")}


@cloud_bucket_app.function(
    name="cloud-bucket-read",
    volumes=[cloud_bucket],
    timeout_seconds=120,
)
def read(key: str) -> CloudBucketSmokeResult:
    return {"key": key, "value": (MOUNT_PATH / key).read_text(encoding="utf-8")}


__all__ = ["cloud_bucket_app", "read", "write_and_read"]
