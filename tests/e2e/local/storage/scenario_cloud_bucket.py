"""Write and read one external object through a Function CloudBucket mount.

Requires an authenticated public lazycloud profile plus
LAZYCLOUD_E2E_STORAGE_OBJECT_ACCESS_KEY and
LAZYCLOUD_E2E_STORAGE_OBJECT_SECRET_KEY. The scenario publicly deletes its
unique app and secrets, and removes only its unique external object prefix.

The supplied S3-compatible endpoint must be reachable by both the client and
worker. Use credentials scoped to this external test bucket.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import Secret

SOURCE_ROOT = Path(__file__).resolve().parent


def _store() -> S3ObjectStoreClient:
    return S3ObjectStoreClient.from_settings(
        S3ObjectStoreSettings(
            endpoint_url=os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_ENDPOINT"],
            bucket=os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_BUCKET"],
            region_name=os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_REGION"],
            access_key_id=os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_ACCESS_KEY"],
            secret_access_key=os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_SECRET_KEY"],
            session_token="",
            force_path_style=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(
            argv,
            description=__doc__ or "CloudBucket mount",
            required_env=(
                "LAZYCLOUD_E2E_STORAGE_OBJECT_ACCESS_KEY",
                "LAZYCLOUD_E2E_STORAGE_OBJECT_SECRET_KEY",
                "LAZYCLOUD_E2E_STORAGE_OBJECT_ENDPOINT",
                "LAZYCLOUD_E2E_STORAGE_OBJECT_BUCKET",
                "LAZYCLOUD_E2E_STORAGE_OBJECT_REGION",
            ),
        )
    except LivePrerequisiteError as exc:
        return blocked(exc)

    workspace = profile.workspace
    suffix = secrets.token_hex(6)
    app_name = f"cloud_bucket_{suffix}"
    prefix = f"e2e/cloud-bucket/{suffix}"
    access_secret_name = f"CLOUD_BUCKET_ACCESS_{suffix.upper()}"
    key_secret_name = f"CLOUD_BUCKET_SECRET_{suffix.upper()}"
    bucket_name = os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_BUCKET"]
    deployment_environment = {
        "LAZYCLOUD_E2E_ACCESS_SECRET": access_secret_name,
        "LAZYCLOUD_E2E_APP": app_name,
        "LAZYCLOUD_E2E_BUCKET": bucket_name,
        "LAZYCLOUD_E2E_BUCKET_INTERNAL_ENDPOINT": os.environ[
            "LAZYCLOUD_E2E_STORAGE_OBJECT_ENDPOINT"
        ],
        "LAZYCLOUD_E2E_BUCKET_PREFIX": prefix,
        "LAZYCLOUD_E2E_BUCKET_REGION": os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_REGION"],
        "LAZYCLOUD_E2E_SECRET_SECRET": key_secret_name,
    }
    previous = {name: os.environ.get(name) for name in deployment_environment}
    os.environ.update(deployment_environment)
    access = Secret(access_secret_name, workspace=workspace)
    secret = Secret(key_secret_name, workspace=workspace)
    access_created = False
    secret_created = False
    store: S3ObjectStoreClient | None = None
    try:
        from . import workload_cloud_bucket as workload

        store = _store()
        access.set(os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_ACCESS_KEY"])
        access_created = True
        secret.set(os.environ["LAZYCLOUD_E2E_STORAGE_OBJECT_SECRET_KEY"])
        secret_created = True
        workload.app.deploy(
            workspace=workspace,
            source_root=SOURCE_ROOT,
            env=deployment_environment,
        )
        marker = f"cloud-bucket-{secrets.token_hex(12)}"
        if workload.cloud_bucket_probe.remote("write", "accepted.txt", marker) != marker:
            raise RuntimeError("CloudBucket mount did not return its write")
        if store.read_bytes(f"{prefix}/accepted.txt") != marker.encode():
            raise RuntimeError("CloudBucket mount write was not durable in external storage")
        if workload.cloud_bucket_probe.remote("read", "accepted.txt") != marker:
            raise RuntimeError("CloudBucket mount did not return its durable object")
        print(
            json.dumps(
                {
                    "app": app_name,
                    "capability": "storage.cloud-bucket",
                    "prefix": prefix,
                }
            )
        )
    finally:
        resources = resource_client(workspace=workspace, timeout_seconds=30)
        apps = [item for item in resources.list_apps(active=True).data if item.name == app_name]
        if len(apps) > 1:
            raise RuntimeError("unique CloudBucket app resolved more than once")
        if apps:
            resources.delete_app(apps[0].id)
        if access_created:
            access.delete()
        if secret_created:
            secret.delete()
        if store is not None:
            store.delete_prefix(f"{prefix}/")
            if store.list_prefix(f"{prefix}/"):
                raise RuntimeError("CloudBucket cleanup left its external object prefix")
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
