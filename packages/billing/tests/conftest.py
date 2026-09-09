import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from coordination.redis_client import RedisSettings
from sqlalchemy.engine import URL
from tests.real_redis import RealRedisActors
from tests.service_fixtures import isolated_services, postgres_database_url, service_graph

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@pytest.fixture
def postgres_services(
    postgres_database_url: URL, tmp_path: Path, real_redis_actors: RealRedisActors
) -> Iterator[ApiServices]:
    settings = DatabaseSettings(
        url=postgres_database_url.render_as_string(hide_password=False),
        application_name=DatabaseApplicationName.Test,
    )
    database = DatabaseClient.from_settings(settings)
    async_io = ApiAsyncIo.from_settings(
        settings,
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        ),
    )
    try:
        with service_graph(
            database,
            tmp_path,
            redis_client=real_redis_actors.client(),
            binary_redis_client=real_redis_actors.client(decode_responses=False),
            async_io=async_io,
        ) as services:
            yield services
    finally:
        asyncio.run(async_io.close())


__all__ = ["isolated_services", "postgres_database_url", "postgres_services"]
