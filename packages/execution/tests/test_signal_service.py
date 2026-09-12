from __future__ import annotations

import pytest
from coordination.redis_client import RedisClient
from execution.signals.redis import RedisSignalRepository, RedisSignalService
from shared.app_identity import REDIS_KEY_PREFIX
from shared.errors import UpstreamUnavailableError
from shared.signals import (
    SignalClearRequest,
    SignalMonitorRequest,
    SignalSetRequest,
    signal_name,
)
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


def test_real_redis_signal_round_trip_ttl_and_workspace_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = RedisSignalService(RedisSignalRepository(redis))

    service.signal_set(
        SignalSetRequest(
            workspace_name="workspace-a",
            name="reload",
            ttl_seconds=30,
        )
    )
    service.signal_set(
        SignalSetRequest(
            workspace_name="workspace-b",
            name="reload",
            ttl_seconds=30,
        )
    )

    present = service.signal_monitor_once(
        SignalMonitorRequest(workspace_name="workspace-a", name="reload")
    )
    assert present.set
    ttl = redis.ttl(redis.key(signal_name("workspace-a", "reload")))
    assert 0 < ttl <= 30

    assert service.delete_workspace("workspace-a") == 1
    assert not service.signal_monitor_once(
        SignalMonitorRequest(workspace_name="workspace-a", name="reload")
    ).set
    assert service.signal_monitor_once(
        SignalMonitorRequest(workspace_name="workspace-b", name="reload")
    ).set


def test_redis_signal_service_surfaces_repository_errors() -> None:
    service = RedisSignalService(
        RedisSignalRepository(RedisClient(_FailingRedis(), key_prefix=REDIS_KEY_PREFIX))
    )

    with pytest.raises(UpstreamUnavailableError):
        service.signal_set(SignalSetRequest(workspace_name="workspace", name="reload"))
    with pytest.raises(UpstreamUnavailableError):
        service.signal_clear(SignalClearRequest(workspace_name="workspace", name="reload"))
    with pytest.raises(UpstreamUnavailableError):
        service.signal_monitor_once(SignalMonitorRequest(workspace_name="workspace", name="reload"))


class _FailingRedis(FakeRedis):
    def set(
        self,
        name: str,
        value: str | bytes | int | float,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        raise RuntimeError("redis down")

    def get(self, name: str) -> str | bytes | None:
        raise RuntimeError("redis down")

    def delete(self, *names: str) -> int:
        raise RuntimeError("redis down")
