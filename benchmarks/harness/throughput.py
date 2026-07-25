from __future__ import annotations

import time
from dataclasses import dataclass

from api.server.services import ApiServices
from coordination.redis_client import RedisClient

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@dataclass(frozen=True)
class ThroughputResult:
    published: int
    consumed: int
    duration_ms: float

    @property
    def messages_per_second(self) -> float:
        if self.duration_ms <= 0:
            return 0.0
        return self.consumed / (self.duration_ms / 1000)


def run_throughput(
    count: int = 100,
    *,
    services: ApiServices | None = None,
) -> ThroughputResult:
    database: DatabaseClient | None = None
    if services is None:
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.Api)
        )
        redis_client = RedisClient.from_settings()
        binary_redis_client = RedisClient.from_settings(decode_responses=False)
        runtime_services = ApiServices.create(
            database,
            redis_client=redis_client,
            binary_redis_client=binary_redis_client,
            owns_redis_client=True,
            owns_binary_redis_client=True,
        )
    else:
        runtime_services = services
    try:
        started = time.perf_counter()
        for index in range(count):
            runtime_services.collections.publish("load", {"index": index})
        consumed = 0
        for _ in range(count):
            message = runtime_services.collections.consume("load")
            if message is None:
                break
            consumed += 1
            runtime_services.collections.ack(message.id, queue=message.queue)
        duration_ms = (time.perf_counter() - started) * 1000
        return ThroughputResult(published=count, consumed=consumed, duration_ms=duration_ms)
    finally:
        if services is None:
            runtime_services.close()
            if database is not None:
                database.dispose()
