from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from agent.binary import AgentBinarySettings
from api.fastapi_app import create_app
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, RedisSettings
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from fastapi.testclient import TestClient
from identity.auth import AuthService
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from shared.identity import WorkspaceRecord
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from storage.volume_filesystem import LocalVolumeFilesystem
from tests.backing_services import redis_url
from tests.database_fixtures import temporary_database
from tests.fakes import FakeObjectClient, FakeWorkspaceBuckets
from tests.real_redis import RealRedisActors
from tests.workspaces import owned_workspace, workspace_owner_user_id

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


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
        object_store_client=FakeObjectClient(),
        workspace_storage_client=FakeWorkspaceBuckets(),
        agent_binary_settings=AgentBinarySettings(
            binary_dir=tmp_path,
            binary_version="test",
            binary_sha256_by_arch={"amd64": "a" * 64},
        ),
        aws_account_connection_settings=AwsAccountConnectionSettings(),
        aws_capacity_settings=AwsCapacitySettings(
            worker_image_digest=f"worker@sha256:{'0' * 64}",
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/test/{'0' * 64}/"
                "lazycloud-agent-linux-amd64"
            ),
            cpu_ami_ids={"us-east-1": "ami-00000000000000000"},
            gpu_ami_ids={"us-east-1": "ami-00000000000000000"},
        ),
    )
    try:
        yield services
    finally:
        services.close()


def _async_io(database: DatabaseClient, actors: RealRedisActors) -> ApiAsyncIo:
    return ApiAsyncIo.from_settings(
        database.settings,
        RedisSettings(
            url=actors.url,
            key_prefix=actors.prefix,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        ),
    )


@contextmanager
def composed_services(
    database: DatabaseClient, root: Path, actors: RealRedisActors
) -> Iterator[ApiServices]:
    async_io = _async_io(database, actors)
    try:
        with service_graph(
            database,
            root,
            redis_client=actors.client(),
            binary_redis_client=actors.client(decode_responses=False),
            async_io=async_io,
        ) as services:
            yield services
    finally:
        asyncio.run(async_io.close())


@pytest.fixture
def isolated_services(
    seeded_database: DatabaseClient, tmp_path: Path, real_redis_actors: RealRedisActors
) -> Iterator[ApiServices]:
    with composed_services(seeded_database, tmp_path, real_redis_actors) as services:
        yield services


@pytest.fixture
async def async_services(
    seeded_database: DatabaseClient, tmp_path: Path, real_redis_actors: RealRedisActors
) -> AsyncIterator[ApiServices]:
    async_io = _async_io(seeded_database, real_redis_actors)
    try:
        await async_io.start()
        with service_graph(
            seeded_database,
            tmp_path,
            redis_client=real_redis_actors.client(),
            binary_redis_client=real_redis_actors.client(decode_responses=False),
            async_io=async_io,
        ) as services:
            yield services
    finally:
        await async_io.close()


@pytest.fixture
def unpriced_services(
    workspace_database: DatabaseClient, tmp_path: Path, real_redis_actors: RealRedisActors
) -> Iterator[ApiServices]:
    """An isolated app graph for scenarios that publish their own rate history."""
    with composed_services(workspace_database, tmp_path, real_redis_actors) as services:
        yield services


@pytest.fixture(scope="session")
def api_runtime(
    postgres_admin: Engine,
    workspace_template_url: URL,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[ApiServices, TestClient]]:
    actors = RealRedisActors(redis_url(), f"lazycloud:test:api:{uuid4().hex}")
    try:
        with temporary_database(postgres_admin, template=workspace_template_url) as url:
            database = DatabaseClient.from_settings(
                DatabaseSettings(
                    url=url.render_as_string(hide_password=False),
                    direct_url=url.render_as_string(hide_password=False),
                    application_name=DatabaseApplicationName.Test,
                )
            )
            try:
                with (
                    composed_services(database, tmp_path_factory.mktemp("api"), actors) as services,
                    TestClient(create_app(services)) as client,
                ):
                    yield services, client
            finally:
                database.dispose()
    finally:
        actors.cleanup()


@pytest.fixture
def api_workspace(api_runtime: tuple[ApiServices, TestClient]) -> WorkspaceRecord:
    services, _ = api_runtime
    return owned_workspace(ControlPlaneService(services.context), f"workspace-{uuid4().hex}")


@pytest.fixture
def api_client(
    api_runtime: tuple[ApiServices, TestClient], api_workspace: WorkspaceRecord
) -> Iterator[TestClient]:
    services, client = api_runtime
    raw_token, _ = AuthService(services.context).create_account_token(
        workspace_owner_user_id(services.context, api_workspace.id), "api-owner"
    )
    client.headers["Authorization"] = f"Bearer {raw_token}"
    client.params = {"workspace": api_workspace.id}
    try:
        yield client
    finally:
        client.headers.pop("Authorization", None)
        client.params = {}
        client.cookies.clear()
        services.auth_token_cache.reset()
