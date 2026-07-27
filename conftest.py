from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import lazycloud.config
import pytest
from tests.real_redis import RealRedisActors

TEST_ENVIRONMENT_FILE = Path(__file__).parent / "tests" / "env.test"


def _test_environment() -> dict[str, str]:
    """Read the suite's declared configuration.

    Resolved from this file rather than the working directory, so it applies
    wherever pytest is invoked from. It lives outside the production settings
    classes deliberately: naming a test file there would let a real process pick
    one up, and an `env_file` fallback only overrides the keys it happens to
    define, leaving the rest to leak from the developer's own `.env`.
    """
    values: dict[str, str] = {}
    for line in TEST_ENVIRONMENT_FILE.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        name, _, value = entry.partition("=")
        values[name.strip()] = value.strip()
    return values


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path))
    # Tests run with an explicitly configured endpoint, exactly like production
    # clients after `lazycloud login` or with LAZYCLOUD_ENDPOINT exported. Tests that
    # assert the endpoint-required contract delete this variable themselves.
    monkeypatch.setenv("LAZYCLOUD_ENDPOINT", "http://127.0.0.1:9000")
    # Settings classes read `.env` from the working directory, so without these
    # the suite silently inherits whatever the developer's machine points at and
    # a test can pass or fail on local configuration. Declare what the suite
    # needs instead; the environment wins over `.env`, so these are what every
    # run sees. Endpoints resolve nowhere on purpose: a unit test that reaches a
    # real object store should fail loudly rather than depend on one running.
    for name, value in _test_environment().items():
        monkeypatch.setenv(name, value)
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
