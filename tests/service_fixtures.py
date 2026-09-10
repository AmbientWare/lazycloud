from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
from agent.binary import AgentBinarySettings
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, RedisSettings
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from identity.auth import TokenIssuer
from identity.users import UserService
from shared.identity import (
    AuthTokenRecord,
    PlatformRole,
    TokenKind,
)
from storage.volume_filesystem import LocalVolumeFilesystem
from storage_client.s3 import S3ObjectStoreSettings

from database import DatabaseClient
from tests.domain_fixtures import owned_workspace
from tests.fakes import FakeObjectClient
from tests.real_redis import RealRedisActors


@dataclass(slots=True)
class _InMemoryWorkspaceBuckets:
    """Provision workspace storage without reaching an object store.

    Workspace storage is mandatory in production, so a workspace created here
    must have it too, or every request that mounts one fails. Only bucket
    creation is faked; the rest of the flow is the production path.
    """

    settings: S3ObjectStoreSettings = field(default_factory=S3ObjectStoreSettings)
    created: list[str] = field(default_factory=list)

    def create_bucket(self, bucket: str | None = None) -> None:
        self.created.append(bucket or self.settings.bucket)

    def validate_bucket_access(self, bucket: str | None = None) -> None:
        del bucket

    def configure_workspace_bucket(self, bucket: str, *, public_origin: str) -> None:
        del bucket, public_origin


@contextmanager
def service_graph(
    database: DatabaseClient,
    tmp_path: Path,
    *,
    redis_client: RedisClient,
    binary_redis_client: RedisClient,
    async_io: ApiAsyncIo,
) -> Iterator[ApiServices]:
    """Compose services over a migrated database. The caller owns async I/O."""

    maps = RedisMapService(binary_redis_client)
    simple_queues = RedisSimpleQueueService(binary_redis_client)
    volume_filesystem = LocalVolumeFilesystem(tmp_path / "volumes")

    services = ApiServices.create(
        database,
        create_schema=False,
        root=tmp_path,
        redis_client=redis_client,
        binary_redis_client=binary_redis_client,
        async_io=async_io,
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
    owned_workspace(ControlPlaneService(services.context), "default")
    services.control_plane_service.ensure_workspace_storage("default")
    try:
        yield services
    finally:
        services.close()


@pytest.fixture
def isolated_services(
    seeded_database: DatabaseClient, tmp_path: Path, real_redis_actors: RealRedisActors
) -> Iterator[ApiServices]:
    async_io = ApiAsyncIo.from_settings(
        seeded_database.settings,
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        ),
    )
    try:
        with service_graph(
            seeded_database,
            tmp_path,
            redis_client=real_redis_actors.client(),
            binary_redis_client=real_redis_actors.client(decode_responses=False),
            async_io=async_io,
        ) as services:
            yield services
    finally:
        asyncio.run(async_io.close())


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
        display_name=f"admin-{uuid4().hex[:12]}",
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
