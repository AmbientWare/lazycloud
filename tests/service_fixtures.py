from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from agent.artifacts import AgentArtifactSettings
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from storage.volume_filesystem import LocalVolumeFilesystem

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from tests.redis_fakes import FakeRedis


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
        agent_artifact_settings=AgentArtifactSettings(
            binary_dir=tmp_path,
            artifact_version="test",
            artifact_sha256_by_arch={"amd64": "a" * 64},
        ),
    )
    ControlPlaneService(services.context).upsert_workspace("default")
    try:
        yield services
    finally:
        services.close()
