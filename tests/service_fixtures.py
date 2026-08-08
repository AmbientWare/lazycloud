from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
from agent.binary import AgentBinarySettings
from api.server.services import ApiServices
from coordination.redis_client import RedisClient
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from identity.auth import TokenIssuer
from identity.users import UserService
from networking.control_plane_origin import RedisControlPlaneOriginRepository
from shared.identity import (
    AuthTokenRecord,
    PlatformRole,
    TokenKind,
)
from storage.volume_filesystem import LocalVolumeFilesystem
from storage_client.s3 import S3ObjectStoreSettings

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from tests.fakes import FakeObjectClient
from tests.redis_fakes import FakeRedis


@dataclass(slots=True)
class _InMemoryWorkspaceBuckets:
    """Provision workspace storage without reaching an object store.

    Workspace storage is mandatory in production, so a workspace created here
    must have it too, or every request that mounts one fails. Only bucket
    creation is faked; the rest of the flow is the production path.
    """

    settings: S3ObjectStoreSettings = field(
        default_factory=lambda: S3ObjectStoreSettings(
            bucket="lazycloud-objects",
            endpoint_url="http://object-store.invalid:9000",
            region_name="us-east-1",
            access_key_id="test-access",
            secret_access_key="test-secret",
            force_path_style=True,
        )
    )
    created: list[str] = field(default_factory=list)

    def create_bucket(self, bucket: str | None = None) -> None:
        self.created.append(bucket or self.settings.bucket)

    def validate_bucket_access(self, bucket: str | None = None) -> None:
        del bucket


@pytest.fixture
def isolated_services(tmp_path: Path) -> Iterator[ApiServices]:
    redis = RedisClient(FakeRedis(), key_prefix="test")
    binary_redis = redis.with_key_prefix("test")
    maps = RedisMapService(binary_redis)
    simple_queues = RedisSimpleQueueService(binary_redis)
    volume_filesystem = LocalVolumeFilesystem(tmp_path / "volumes")

    services = ApiServices.create(
        DatabaseClient.from_settings(
            DatabaseSettings(
                url="sqlite+pysqlite:///:memory:",
                application_name=DatabaseApplicationName.Test,
            )
        ),
        root=tmp_path,
        redis_client=redis,
        binary_redis_client=binary_redis,
        owns_redis_client=False,
        owns_binary_redis_client=False,
        map_service=maps,
        simple_queue_service=simple_queues,
        volume_filesystem=volume_filesystem,
        # Without this the graph wires a real S3 client at the configured
        # endpoint, so a unit test that touches object storage reaches out over
        # the network instead of failing on its own terms.
        object_store_client=FakeObjectClient(),
        workspace_storage_client=_InMemoryWorkspaceBuckets(),
        agent_binary_settings=AgentBinarySettings(
            binary_dir=tmp_path,
            binary_version="test",
            binary_sha256_by_arch={"amd64": "a" * 64},
        ),
    )
    # A running control plane publishes where it is reachable during startup,
    # and everything that hands that address onward reads it back. These
    # services are built without that startup, so the fixture stands in for it.
    RedisControlPlaneOriginRepository(redis).publish(
        services.gateway_settings.runtime_callback_http_url
    )
    services.control_plane_service.upsert_workspace("default")
    services.control_plane_service.ensure_workspace_storage("default")
    try:
        yield services
    finally:
        services.close()


def administrator_credential(
    services: ApiServices,
    name: str = "administrator",
) -> tuple[str, AuthTokenRecord]:
    """An administrator credential, made the only way production makes one.

    Administrator standing belongs to the account, not to the shape of a token, so
    this creates a user whose role says so and mints a credential naming them. No
    membership: a platform administrator reaches every workspace without one, and
    granting it would collide with whatever owner the test set up itself.
    """
    user = UserService(services.context).create(
        username=f"admin-{uuid4().hex[:12]}",
        password="administrator-fixture-password",
        role=PlatformRole.Administrator,
    )
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, record = issuer.issue_for_user(
            session,
            name,
            user_id=user.id,
            kind=TokenKind.Admin,
        )
    issuer.committed()
    return raw_token, record
