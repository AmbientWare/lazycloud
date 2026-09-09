from __future__ import annotations

import anyio
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from database import AsyncDatabaseClient, DatabaseApplicationName, DatabaseSettings


def test_cancelled_transaction_rolls_back_and_releases_connection(
    postgres_database_url: URL,
) -> None:
    async def scenario() -> None:
        database = AsyncDatabaseClient.from_settings(
            DatabaseSettings(
                url=postgres_database_url.render_as_string(hide_password=False),
                application_name=DatabaseApplicationName.Test,
                pool_size=1,
                max_overflow=0,
            )
        )
        try:
            await database.run_transaction(
                lambda session: session.execute(
                    text("CREATE TABLE cancellation_probe (value integer PRIMARY KEY)")
                )
            )

            with anyio.CancelScope() as cancelled:

                def interrupted(session: Session) -> None:
                    session.execute(text("INSERT INTO cancellation_probe VALUES (1)"))
                    cancelled.cancel()
                    session.execute(text("SELECT pg_sleep(0.01)"))

                await database.run_transaction(interrupted)

            assert cancelled.cancelled_caught
            status = database.pool_status()
            assert status is not None and status.checked_out == 0
            assert (
                await database.run_transaction(
                    lambda session: session.scalar(text("SELECT count(*) FROM cancellation_probe"))
                )
                == 0
            )

            await database.run_transaction(
                lambda session: session.execute(text("INSERT INTO cancellation_probe VALUES (2)"))
            )
            assert (
                await database.run_transaction(
                    lambda session: session.scalar(text("SELECT value FROM cancellation_probe"))
                )
                == 2
            )
        finally:
            await database.dispose()

    anyio.run(scenario)
