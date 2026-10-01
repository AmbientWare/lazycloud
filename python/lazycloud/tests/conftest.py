from __future__ import annotations

from collections.abc import Iterator

import lazycloud.config
import pytest

from tests.api_server import TOKEN, WORKSPACE, FakeApi, running_fake_api


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
