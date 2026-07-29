from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from agent.binary import AgentBinarySettings
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from tests.redis_fakes import FakeRedis

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


@pytest.fixture
def isolated_services(tmp_path: Path) -> Iterator[ApiServices]:
    redis = RedisClient(FakeRedis(), key_prefix="test")
    binary_redis = redis.with_key_prefix("test")
    maps = RedisMapService(binary_redis)
    simple_queues = RedisSimpleQueueService(binary_redis)

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
        agent_binary_settings=AgentBinarySettings(
            binary_dir=tmp_path,
            artifact_version="test",
            sha256_by_arch={"amd64": "0" * 64},
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
            instance_hourly_micros={"test.instance": 0},
        ),
        map_service=maps,
        simple_queue_service=simple_queues,
    )
    ControlPlaneService(services.context).upsert_workspace("default")
    try:
        yield services
    finally:
        services.close()
