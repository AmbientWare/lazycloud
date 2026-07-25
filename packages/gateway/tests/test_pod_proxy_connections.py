from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from coordination.redis_client import RedisClient, RedisWireScalar
from execution.pods.proxy import PodProxyHttpRequest, PodProxyTarget
from gateway.pod_proxy import PodProxyHttpClient, RedisPodProxyConnectionRepository
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


def test_connection_decrement_deletes_zero_keys_and_never_creates_negative_state() -> None:
    transport = _AtomicCounterRedis()
    redis = RedisClient(transport, key_prefix="pod-proxy-test")
    repository = RedisPodProxyConnectionRepository(redis)
    container_key = _container_key(redis)
    total_key = _total_key(redis)

    assert (
        repository.increment_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        )
        == 1
    )
    assert (
        repository.increment_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        )
        == 2
    )
    assert (
        repository.decrement_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        )
        == 1
    )
    assert redis.get(container_key) == "1"
    assert (
        repository.decrement_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        )
        == 0
    )
    assert not redis.exists(container_key)
    assert (
        repository.decrement_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        )
        == 0
    )
    assert not redis.exists(container_key)

    assert repository.increment_total_connections("workspace", "stub") == 1
    assert repository.decrement_total_connections("workspace", "stub") == 0
    assert not redis.exists(total_key)
    assert repository.decrement_total_connections("workspace", "stub") == 0
    assert not redis.exists(total_key)


def test_connection_open_only_persists_an_existing_keep_warm_lock() -> None:
    transport = _AtomicCounterRedis()
    redis = RedisClient(transport, key_prefix="pod-proxy-keep-warm")
    repository = RedisPodProxyConnectionRepository(redis)
    lock_key = redis.key(pod_keep_warm_lock_key("workspace", "stub", "container"))

    repository.increment_container_connections(
        "workspace", "stub", "container", keep_warm_seconds=30
    )
    assert not redis.exists(lock_key)

    redis.set(lock_key, "1", ex=1)
    repository.increment_container_connections(
        "workspace", "stub", "container", keep_warm_seconds=30
    )
    assert redis.ttl(lock_key) == -1


def test_real_redis_connection_counters_are_atomic_and_leave_no_idle_keys(
    real_redis_actors: RealRedisActors,
) -> None:
    repositories = [
        RedisPodProxyConnectionRepository(real_redis_actors.client()) for _index in range(8)
    ]

    _assert_concurrent_counter_lifecycle(repositories, real_redis_actors.clients[0])

    repositories[0].increment_container_connections(
        "workspace", "stub", "container", keep_warm_seconds=0
    )
    repositories[0].increment_total_connections("workspace", "stub")
    with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
        list(
            executor.map(
                _increment_then_decrement_both,
                [repositories[index % len(repositories)] for index in range(64)],
            )
        )

    assert repositories[0].container_connections("workspace", "stub", "container") == 1
    assert real_redis_actors.clients[0].get(_total_key(real_redis_actors.clients[0])) == "1"
    _decrement_both(repositories[0])
    assert not real_redis_actors.clients[0].exists(_container_key(real_redis_actors.clients[0]))
    assert not real_redis_actors.clients[0].exists(_total_key(real_redis_actors.clients[0]))


def test_real_redis_connection_open_cannot_recreate_a_deleted_keep_warm_lock(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisPodProxyConnectionRepository(redis)
    lock_key = redis.key(pod_keep_warm_lock_key("workspace", "stub", "container"))

    repository.increment_container_connections(
        "workspace", "stub", "container", keep_warm_seconds=30
    )

    assert not redis.exists(lock_key)


def test_pinned_direct_http_connect_uses_its_separate_dial_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float | None] = []

    def fail_connect(connection: object) -> None:
        observed.append(getattr(connection, "timeout", None))
        raise TimeoutError("dial timed out")

    monkeypatch.setattr("gateway.pod_proxy.http.client.HTTPConnection.connect", fail_connect)

    with pytest.raises(TimeoutError, match="dial timed out"):
        PodProxyHttpClient().forward(
            PodProxyTarget(container_id="sandbox", address="127.0.0.1:8080"),
            PodProxyHttpRequest(stub_id="stub", port=8080, method="GET"),
            timeout_seconds=175.0,
            connect_timeout_seconds=1.0,
        )

    assert observed == [1.0]


def _assert_concurrent_counter_lifecycle(
    repositories: list[RedisPodProxyConnectionRepository],
    redis: RedisClient,
) -> None:
    operations = 64
    with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
        increments = list(
            executor.map(
                _increment_both,
                [repositories[index % len(repositories)] for index in range(operations)],
            )
        )

    assert sorted(container for container, _total in increments) == list(range(1, operations + 1))
    assert sorted(total for _container, total in increments) == list(range(1, operations + 1))
    assert repositories[0].container_connections("workspace", "stub", "container") == operations

    with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
        decrements = list(
            executor.map(
                _decrement_both,
                [repositories[index % len(repositories)] for index in range(operations)],
            )
        )

    assert sorted(container for container, _total in decrements) == list(range(operations))
    assert sorted(total for _container, total in decrements) == list(range(operations))
    assert repositories[0].container_connections("workspace", "stub", "container") == 0
    assert not redis.exists(_container_key(redis))
    assert not redis.exists(_total_key(redis))


def _container_key(redis: RedisClient) -> str:
    return redis.key(pod_container_connections_key("workspace", "stub", "container"))


def _total_key(redis: RedisClient) -> str:
    return redis.key(pod_total_connections_key("workspace", "stub"))


def _increment_both(repository: RedisPodProxyConnectionRepository) -> tuple[int, int]:
    return (
        repository.increment_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        ),
        repository.increment_total_connections("workspace", "stub"),
    )


def _decrement_both(repository: RedisPodProxyConnectionRepository) -> tuple[int, int]:
    return (
        repository.decrement_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        ),
        repository.decrement_total_connections("workspace", "stub"),
    )


def _increment_then_decrement_both(repository: RedisPodProxyConnectionRepository) -> None:
    _increment_both(repository)
    _decrement_both(repository)


class _AtomicCounterRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.eval_lock = threading.Lock()

    def incr(self, name: str) -> int:
        with self.eval_lock:
            return super().incr(name)

    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisWireScalar,
    ) -> int:
        if 'redis.call("INCR", KEYS[1])' in script:
            assert numkeys == 2
            counter_key = str(keys_and_args[0])
            keep_warm_key = str(keys_and_args[1])
            keep_warm_seconds = int(keys_and_args[2])
            with self.eval_lock:
                count = super().incr(counter_key)
                if keep_warm_seconds > 0:
                    if self.exists(keep_warm_key):
                        self.expirations.pop(keep_warm_key, None)
                elif keep_warm_seconds == 0:
                    self.delete(keep_warm_key)
                return count
        assert 'redis.call("GET", KEYS[1])' in script
        assert 'redis.call("DEL", KEYS[1])' in script
        assert 'redis.call("DECR", KEYS[1])' in script
        assert numkeys in {1, 2}
        key = str(keys_and_args[0])
        with self.eval_lock:
            value = self.get(key)
            if value is None:
                return 0
            count = int(value)
            if count <= 1:
                self.delete(key)
                if numkeys == 2:
                    keep_warm_key = str(keys_and_args[1])
                    keep_warm_seconds = int(keys_and_args[2])
                    if keep_warm_seconds > 0:
                        self.expire(keep_warm_key, keep_warm_seconds)
                return 0
            return self.decr(key)
