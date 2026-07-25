from __future__ import annotations

import pytest
from sqlalchemy import text

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseReadinessTimeoutError,
    DatabaseSettings,
    repository_database_head,
    wait_for_database_head,
)


def test_database_readiness_requires_exact_repository_head() -> None:
    client = DatabaseClient.from_settings(
        DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            application_name=DatabaseApplicationName.Test,
        )
    )
    expected_revision = repository_database_head()
    try:
        with client.engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255))"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": expected_revision},
            )

        readiness = wait_for_database_head(
            client,
            timeout_seconds=0.1,
            poll_interval_seconds=0.01,
        )

        assert readiness.revision == expected_revision
        assert readiness.attempts == 1
        assert readiness.elapsed_seconds >= 0
    finally:
        client.dispose()


def test_database_readiness_times_out_with_typed_observation() -> None:
    client = DatabaseClient.from_settings(
        DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with pytest.raises(DatabaseReadinessTimeoutError) as raised:
            wait_for_database_head(
                client,
                timeout_seconds=0.01,
                poll_interval_seconds=0.002,
            )

        error = raised.value
        assert error.expected_revision == repository_database_head()
        assert error.observed_revisions == ()
        assert error.attempts >= 1
        assert error.last_error_type == "OperationalError"
        assert "observed revisions: none" in str(error)
    finally:
        client.dispose()


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_interval_seconds", "message"),
    [
        (0.0, 1.0, "timeout_seconds must be greater than zero"),
        (1.0, 0.0, "poll_interval_seconds must be greater than zero"),
    ],
)
def test_database_readiness_rejects_unbounded_polling_inputs(
    timeout_seconds: float,
    poll_interval_seconds: float,
    message: str,
) -> None:
    client = DatabaseClient.from_settings(
        DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with pytest.raises(ValueError, match=message):
            wait_for_database_head(
                client,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
    finally:
        client.dispose()
