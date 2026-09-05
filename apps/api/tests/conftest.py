from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from agent.binary import AgentBinarySettings
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisSettings
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from storage.workspace_storage_issuers import StoredWorkspaceStorageIssuer
from tests.real_redis import RealRedisActors
from tests.service_fixtures import owned_workspace

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


@pytest.fixture
def isolated_services(
    tmp_path: Path,
    real_redis_actors: RealRedisActors,
) -> Iterator[ApiServices]:
    redis = real_redis_actors.client()
    binary_redis = real_redis_actors.client(decode_responses=False)
    maps = RedisMapService(binary_redis)
    simple_queues = RedisSimpleQueueService(binary_redis)
    database_path = tmp_path / "api.sqlite3"
    database_settings = DatabaseSettings(
        url=f"sqlite+pysqlite:///{database_path}",
        application_name=DatabaseApplicationName.Test,
    )
    async_io = ApiAsyncIo.from_settings(
        DatabaseSettings(
            url=f"sqlite+aiosqlite:///{database_path}",
            application_name=DatabaseApplicationName.Test,
        ),
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        ),
    )

    services = ApiServices.create(
        DatabaseClient.from_settings(database_settings),
        root=tmp_path,
        redis_client=redis,
        binary_redis_client=binary_redis,
        async_io=async_io,
        owns_redis_client=False,
        owns_binary_redis_client=False,
        workspace_storage_issuer=StoredWorkspaceStorageIssuer(),
        agent_binary_settings=AgentBinarySettings(
            binary_dir=tmp_path,
            binary_version="test",
            binary_sha256_by_arch={"amd64": "0" * 64},
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
            instance_hourly_micros={"test.instance": 1},
        ),
        map_service=maps,
        simple_queue_service=simple_queues,
    )
    owned_workspace(ControlPlaneService(services.context), "default")
    try:
        yield services
    finally:
        services.close()
