from __future__ import annotations

import pytest
from sqlalchemy.engine import URL

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseReadinessTimeoutError,
    DatabaseSettings,
    repository_database_head,
    wait_for_database_head,
)


def test_database_readiness_requires_exact_repository_head(database: DatabaseClient) -> None:
    readiness = wait_for_database_head(database, timeout_seconds=0.1, poll_interval_seconds=0.01)
    assert readiness.revision == repository_database_head()


def test_database_readiness_times_out_with_typed_observation(
    postgres_database_url: URL,
) -> None:
    client = DatabaseClient.from_settings(
        DatabaseSettings(
            url=postgres_database_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with pytest.raises(DatabaseReadinessTimeoutError) as raised:
            wait_for_database_head(client, timeout_seconds=0.01, poll_interval_seconds=0.002)
        error = raised.value
        assert error.expected_revision == repository_database_head()
        assert error.observed_revisions == ()
        assert error.attempts >= 1
        assert error.last_error_type == "ProgrammingError"
    finally:
        client.dispose()


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_interval_seconds"),
    [(0.0, 1.0), (1.0, 0.0)],
)
def test_database_readiness_rejects_unbounded_polling_inputs(
    database: DatabaseClient,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        wait_for_database_head(
            database,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
