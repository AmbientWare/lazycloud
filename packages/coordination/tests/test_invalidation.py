from __future__ import annotations

import pytest
from coordination.invalidation import RedisInvalidationGeneration
from coordination.redis_client import RedisClient
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.redis_fakes import FakeRedis


def test_invalidation_generation_reads_bytes_and_propagates_redis_errors() -> None:
    fake = _CounterRedis()
    fake.values["test:invalidation:auth-tokens"] = b"7"
    generation = RedisInvalidationGeneration(
        redis=RedisClient(fake, key_prefix="test"),
        scope="auth-tokens",
    )

    assert generation.current() == 7

    fake.fail = True
    with pytest.raises(RedisConnectionError):
        generation.current()
    with pytest.raises(RedisConnectionError):
        generation.bump()


class _CounterRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def get(self, name: str) -> str | bytes | int | float | bool | None:
        if self.fail:
            raise RedisConnectionError("redis unavailable")
        return self.values.get(name)

    def incr(self, name: str) -> int:
        if self.fail:
            raise RedisConnectionError("redis unavailable")
        value = int(self.values.get(name, 0)) + 1
        self.values[name] = str(value)
        return value
