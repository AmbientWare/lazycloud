from __future__ import annotations

from typing import Annotated

import typer
from lazycloud.cli.components.output import print_payload

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSchemaInspection,
    DatabaseSettings,
    bootstrap_database,
    inspect_database_schema,
    wait_for_database_head,
)

database_app = typer.Typer(help="Inspect and initialize the current database schema.")


@database_app.command("check")
def database_check(ctx: typer.Context) -> None:
    client = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    try:
        payload: dict[str, str | bool] = {
            "healthy": client.ping(),
            "backend": str(client.engine.dialect.name),
            "driver": str(client.engine.dialect.driver),
        }
    finally:
        client.dispose()
    print_payload(ctx, payload)


@database_app.command("status")
def database_status(ctx: typer.Context) -> None:
    print_payload(ctx, _schema_payload(inspect_database_schema()))


@database_app.command("initialize")
def database_initialize(ctx: typer.Context) -> None:
    print_payload(ctx, _schema_payload(bootstrap_database()))


@database_app.command("wait")
def database_wait(
    ctx: typer.Context,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0.1, help="Maximum time to wait."),
    ] = 600.0,
    poll_interval_seconds: Annotated[
        float,
        typer.Option("--poll-interval-seconds", min=0.05, help="Readiness poll interval."),
    ] = 1.0,
) -> None:
    client = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Wait)
    )
    try:
        readiness = wait_for_database_head(
            client,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        payload: dict[str, str | int | float | bool] = {
            "healthy": True,
            "revision": readiness.revision,
            "attempts": readiness.attempts,
            "elapsed_seconds": readiness.elapsed_seconds,
        }
    finally:
        client.dispose()
    print_payload(ctx, payload)


def _schema_payload(
    inspection: DatabaseSchemaInspection,
) -> dict[str, str | list[str] | None]:
    return {
        "state": inspection.state.value,
        "current_revision": inspection.current_revision,
        "observed_revisions": list(inspection.observed_revisions),
        "target_revision": inspection.target_revision,
    }


__all__ = ["database_app"]
