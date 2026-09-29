from __future__ import annotations

import socket
from dataclasses import replace

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import AsyncRedisClient, RedisClient, RedisSettings
from observability.stream_state import AsyncRedisEventStreamRepository, RedisEventStreamRepository
from shared.tasks import TaskStatus


@pytest.mark.anyio
async def test_redis_publication_outage_preserves_task_admission_and_completion(
    async_services: ApiServices,
) -> None:
    stub = ControlPlaneService(async_services.context).create_stub(
        "publication-outage", kind=StubKind.Endpoint
    )
    with socket.socket() as unavailable_port:
        unavailable_port.bind(("127.0.0.1", 0))
        settings = RedisSettings(url=f"redis://127.0.0.1:{unavailable_port.getsockname()[1]}/0")
        redis = RedisClient.from_settings(settings)
        async_redis = AsyncRedisClient.from_settings(settings)
        tasks = replace(
            async_services.tasks,
            log_streams=RedisEventStreamRepository(redis=redis),
            async_log_streams=AsyncRedisEventStreamRepository(redis=async_redis),
        )
        try:
            task = tasks.create("accepted", workspace_id=stub.workspace_id, stub_id=stub.id)
            outcome = await tasks.finish_with_retry_async(
                task.id, TaskStatus.Complete, result={"value": 7}, exit_code=0
            )
            assert outcome.task.status is TaskStatus.Complete
            saved = await async_services.tasks.get_async(task.id)
            assert saved.status is TaskStatus.Complete
            assert saved.result == {"value": 7}
        finally:
            redis.close()
            await async_redis.close()
