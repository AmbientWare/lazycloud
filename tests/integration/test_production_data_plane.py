from __future__ import annotations

import hashlib
from base64 import b64encode
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from boto3.s3.transfer import TransferConfig
from botocore.awsrequest import AWSRequest
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository
from shared.app_lifecycle import AppLifecycleState
from shared.deployment_records import DeploymentSpec
from sqlalchemy.exc import IntegrityError
from storage.volume_filesystem import LocalVolumeFilesystem
from storage_client.s3 import (
    _add_delete_objects_content_md5,
    presign_endpoint_for_storage,
)
from tests.redis_fakes import FakeRedis

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)


class _UploadExtraArgs(TypedDict):
    ContentType: str
    Metadata: dict[str, str]


def _services(root: Path) -> ApiServices:
    redis = RedisClient(FakeRedis(), key_prefix="test")
    services = ApiServices.create(
        DatabaseClient.from_settings(
            DatabaseSettings(
                url="sqlite+pysqlite:///:memory:",
                application_name=DatabaseApplicationName.Test,
            )
        ),
        root=root,
        redis_client=redis,
        binary_redis_client=redis.with_key_prefix("test"),
        volume_filesystem=LocalVolumeFilesystem(root / "volumes"),
    )
    ControlPlaneService(services.context).upsert_workspace("default")
    return services


def test_deployment_versions_continue_after_soft_delete(tmp_path: Path) -> None:
    services = _services(tmp_path)
    app = services.apps.create("demo_soft_delete")
    first = services.deployments.deploy(
        DeploymentSpec(
            name="demo",
            handler="module:function",
            metadata={"app_id": app.id},
        )
    )

    deleted = services.deployments.delete(first.id)
    second = services.deployments.deploy(
        DeploymentSpec(
            name="demo",
            handler="module:function",
            metadata={"app_id": app.id},
        )
    )

    assert deleted.version == 1
    assert second.version == 2
    assert services.deployments.list(app_id=app.id) == [second]


def test_active_app_names_are_unique_per_workspace(tmp_path: Path) -> None:
    services = _services(tmp_path)
    active = services.apps.create("api")

    assert active.name == "api"

    with (
        pytest.raises(IntegrityError),
        services.context.database.session() as session,
    ):
        AppRepository(session).upsert(
            AppRecord(
                id=str(uuid4()),
                workspace_id=active.workspace_id,
                name="api",
            )
        )

    with services.context.database.session() as session:
        deleted = AppRepository(session).upsert(
            AppRecord(
                id=str(uuid4()),
                workspace_id=active.workspace_id,
                name="api",
                lifecycle_state=AppLifecycleState.Deleted,
                deleted_at=datetime.now(UTC),
            )
        )

    assert deleted.name == "api"
    assert deleted.deleted_at is not None


def test_s3_presign_endpoint_matches_storage_endpoint_policy() -> None:
    assert (
        presign_endpoint_for_storage("https://s3.amazonaws.com", None) == "https://s3.amazonaws.com"
    )
    assert (
        presign_endpoint_for_storage("http://object-store:9000", "http://127.0.0.1:9002")
        == "http://127.0.0.1:9002"
    )
    assert (
        presign_endpoint_for_storage("http://garage:9000", "http://localhost:9002")
        == "http://localhost:9002"
    )
    assert (
        presign_endpoint_for_storage("https://s3.amazonaws.com", "http://127.0.0.1:9002")
        == "https://s3.amazonaws.com"
    )
    assert (
        presign_endpoint_for_storage("http://garage.internal:9000", "https://storage.example.com")
        == "https://storage.example.com"
    )


def test_s3_delete_objects_requests_include_content_md5() -> None:
    body = b"<Delete><Object><Key>volumes/data/a.txt</Key></Object></Delete>"
    request = AWSRequest(method="POST", url="https://s3.example/lazycloud?delete", data=body)

    _add_delete_objects_content_md5(request)

    assert request.headers["Content-MD5"] == b64encode(
        hashlib.md5(body, usedforsecurity=False).digest()
    ).decode("ascii")


class _RecordingS3TransferClient:
    def __init__(self, download_payload: bytes = b"") -> None:
        self.download_payload = download_payload
        self.uploads: list[tuple[str, str, str, _UploadExtraArgs, TransferConfig]] = []
        self.downloads: list[tuple[str, str, str]] = []

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        *,
        ExtraArgs: _UploadExtraArgs,
        Config: TransferConfig,
    ) -> None:
        self.uploads.append((Filename, Bucket, Key, ExtraArgs, Config))

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        target = Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.download_payload)
        self.downloads.append((bucket, key, filename))
