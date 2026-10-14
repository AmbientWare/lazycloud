from __future__ import annotations

import weakref
from collections.abc import Iterator

import lazycloud.config
import pytest
import typer
from typer.main import get_command

from tests.api_server import TOKEN, WORKSPACE, FakeApi, running_fake_api


@pytest.fixture(scope="session", autouse=True)
def cli_converted_once() -> Iterator[None]:
    """Convert each CLI app to its click command once.

    CliRunner converts the whole command tree on every invoke, which costs
    more than most commands under test. Reuse an app object to share it.
    """
    converted: weakref.WeakKeyDictionary[typer.Typer, object] = weakref.WeakKeyDictionary()

    def convert(app: typer.Typer) -> object:
        if app not in converted:
            converted[app] = get_command(app)
        return converted[app]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("typer.testing._get_command", convert)
        yield


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    """A running fake API with the client profile pointed at it."""
    with running_fake_api() as api:
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", api.url)
        monkeypatch.setenv("LAZYCLOUD_TOKEN", TOKEN)
        monkeypatch.setenv("LAZYCLOUD_WORKSPACE", WORKSPACE)
        lazycloud.config.reset_settings_cache()
        yield api
    lazycloud.config.reset_settings_cache()
