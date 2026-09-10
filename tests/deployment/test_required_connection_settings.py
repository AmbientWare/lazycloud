from __future__ import annotations

from collections.abc import Callable

import pytest
from coordination.redis_client import RedisSettings
from shared.deployment_settings import MissingDeploymentSettingError
from storage_client.s3 import S3ObjectStoreSettings

from database import DatabaseApplicationName, DatabaseSettings

# Each row is a distinct process-startup path to the wrong datastore. A default
# here is silent by construction: the process connects, the writes land
# somewhere, and nothing reports a problem until the data is read back from a
# store nobody meant to use.
type ConnectionSettings = DatabaseSettings | RedisSettings | S3ObjectStoreSettings

_CONNECTION_SETTINGS: tuple[tuple[str, str, Callable[[], ConnectionSettings]], ...] = (
    (
        "database",
        "LAZYCLOUD_DATABASE_URL",
        lambda: DatabaseSettings(application_name=DatabaseApplicationName.Api),
    ),
    ("redis", "LAZYCLOUD_REDIS_URL", RedisSettings),
    ("object-store", "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL", S3ObjectStoreSettings),
)


@pytest.mark.parametrize(
    ("variable", "build"),
    [pytest.param(variable, build, id=name) for name, variable, build in _CONNECTION_SETTINGS],
)
def test_unset_connection_setting_fails_by_name(
    variable: str,
    build: Callable[[], ConnectionSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(variable, raising=False)

    with pytest.raises(MissingDeploymentSettingError) as raised:
        build()

    assert raised.value.variable == variable
    assert variable in str(raised.value)
