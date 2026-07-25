from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import lazycloud.config
import pytest
from tests.real_redis import RealRedisActors


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path))
    # Tests run with an explicitly configured endpoint, exactly like production
    # clients after `lazycloud login` or with LAZYCLOUD_ENDPOINT exported. Tests that
    # assert the endpoint-required contract delete this variable themselves.
    monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "http://127.0.0.1:9000")
    lazycloud.config.reset_settings_cache()
    yield
    lazycloud.config.reset_settings_cache()


@pytest.fixture
def real_redis_actors() -> Iterator[RealRedisActors]:
    url = os.environ.get("LAZYCLOUD_TEST_REDIS_URL")
    if not url:
        pytest.skip("LAZYCLOUD_TEST_REDIS_URL is required for real Redis repository acceptance")
    actors = RealRedisActors(
        url=url,
        prefix=f"lazycloud:test:redis-acceptance:{uuid4()}",
    )
    actors.client()
    try:
        yield actors
    finally:
        actors.cleanup()
