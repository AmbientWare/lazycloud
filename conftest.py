from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import lazycloud.config
import pytest
from tests.metric_helpers import install_metric_reader
from tests.real_redis import RealRedisActors

TEST_ENVIRONMENT_FILE = Path(__file__).parent / "tests" / "env.test"

# Cleared from the environment before the suite declares its own configuration.
# `LAZYCLOUD_TEST_` is exempt: those name the real Redis and PostgreSQL services
# an opt-in test tier connects to, and the harness must not remove them.
_INHERITED_PREFIXES = ("LAZYCLOUD_", "AWS_")
_RETAINED_PREFIX = "LAZYCLOUD_TEST_"
# Typer renders its help and errors through Rich, which colourises whenever any
# of these is set — a developer's shell sets FORCE_COLOR, and CI sets
# GITHUB_ACTIONS. Assertions on CLI output would then pass on a bare terminal
# and fail everywhere else, so the suite states its own answer rather than
# inheriting one.
_FORCED_COLOR_VARIABLES = ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE", "GITHUB_ACTIONS")

# Cleared at import, not only per test: Rich decides `force_terminal` once when a
# Console is constructed, and the CLI's console is a module-level singleton built
# the moment a test imports it. A fixture would run too late to change it.
for _name in _FORCED_COLOR_VARIABLES:
    os.environ.pop(_name, None)


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


@pytest.fixture(scope="session", autouse=True)
def metric_reader() -> None:
    """Install the session's meter provider before anything records.

    Once, because the API refuses a second one. Tests that assert on metrics read
    through `tests.metric_helpers.metric_value`.
    """

    install_metric_reader()


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Clear first, then declare. A positive list cannot express "unset", and it
    # silently grows stale as settings classes are added; clearing the prefixes
    # outright makes the suite's configuration exactly what this file states,
    # whatever the developer's shell or `.env` happens to hold.
    for name in tuple(os.environ):
        if name.startswith(_INHERITED_PREFIXES) and not name.startswith(_RETAINED_PREFIX):
            monkeypatch.delenv(name, raising=False)
    for name in _FORCED_COLOR_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAZYCLOUD_HOME", str(tmp_path))
    # Endpoints resolve nowhere on purpose: a unit test that reaches a real
    # object store should fail loudly rather than depend on one running.
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
