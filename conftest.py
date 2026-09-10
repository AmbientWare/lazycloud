from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import lazycloud.config
import pytest
from tests.metric_helpers import install_metric_reader

TEST_ENVIRONMENT_FILE = Path(__file__).parent / "tests" / "env.test"
pytest_plugins = ["tests.database_fixtures", "tests.redis_fixtures", "tests.timings"]

# Preserve the real PostgreSQL and Redis endpoints while isolating other settings.
_INHERITED_PREFIXES = ("LAZYCLOUD_", "AWS_")
_RETAINED_PREFIX = "LAZYCLOUD_TEST_"
# Rich must render CLI output consistently in local shells and CI.
_FORCED_COLOR_VARIABLES = ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE", "GITHUB_ACTIONS")

# Rich constructs the CLI console at import time, before fixtures can clear these.
for _name in _FORCED_COLOR_VARIABLES:
    os.environ.pop(_name, None)


def _test_environment() -> dict[str, str]:
    """Read test configuration without adding a production settings fallback."""
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
    """Install one meter provider; readers use tests.metric_helpers.metric_value."""

    install_metric_reader()


_TEST_ENVIRONMENT = _test_environment()


def _configure_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    # Clearing the prefixes also isolates settings absent from env.test.
    for name in tuple(os.environ):
        if name.startswith(_INHERITED_PREFIXES) and not name.startswith(_RETAINED_PREFIX):
            monkeypatch.delenv(name, raising=False)
    for name in _FORCED_COLOR_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAZYCLOUD_HOME", str(home))
    # Unconfigured external services must fail instead of reaching developer accounts.
    for name, value in _TEST_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    lazycloud.config.reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def suite_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        _configure_environment(monkeypatch, tmp_path_factory.mktemp("suite"))
        yield
    lazycloud.config.reset_settings_cache()


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _configure_environment(monkeypatch, tmp_path)
    yield
    lazycloud.config.reset_settings_cache()


@pytest.fixture
def isolated_imports(tmp_path: Path) -> Iterator[None]:
    """Unload test-authored modules and restore the caller's import search path."""
    search_path = sys.path.copy()
    previous_modules = sys.modules.copy()
    try:
        yield
    finally:
        sys.path[:] = search_path
        for name, module in previous_modules.items():
            if name not in sys.modules:
                sys.modules[name] = module
        temporary_modules: list[str] = []
        for name, module in tuple(sys.modules.items()):
            if previous_modules.get(name) is module or not isinstance(module, ModuleType):
                continue
            spec = module.__spec__
            locations = list(spec.submodule_search_locations or ()) if spec else []
            if spec and spec.origin:
                locations.append(spec.origin)
            source = getattr(module, "__file__", None)
            if isinstance(source, str):
                locations.append(source)
            if any(Path(location).is_relative_to(tmp_path) for location in locations):
                temporary_modules.append(name)
        for name in temporary_modules:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)
