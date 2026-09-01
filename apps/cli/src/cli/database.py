from __future__ import annotations

from typing import Annotated

import typer
from lazycloud.cli.components.formatting import duration
from lazycloud.cli.components.output import console
from lazycloud.cli.components.results import emit_result

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseReadinessProbe,
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
    emit_result(
        ctx,
        payload=payload,
        title="Database connection",
        fields={"healthy": payload["healthy"], "backend": payload["backend"]},
        tone="success" if payload["healthy"] else "warning",
    )


@database_app.command("status")
def database_status(ctx: typer.Context) -> None:
    inspection = inspect_database_schema()
    _show_schema(ctx, inspection, title="Database schema")


@database_app.command("initialize")
def database_initialize(ctx: typer.Context) -> None:
    inspection = bootstrap_database()
    _show_schema(ctx, inspection, title="Database initialized")


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
            on_poll=_show_readiness_probe,
        )
        payload: dict[str, str | int | float | bool] = {
            "healthy": True,
            "revision": readiness.revision,
            "attempts": readiness.attempts,
            "elapsed_seconds": readiness.elapsed_seconds,
        }
    finally:
        client.dispose()
    emit_result(
        ctx,
        payload=payload,
        title="Database ready",
        fields={
            "revision": readiness.revision,
            "attempts": readiness.attempts,
            "elapsed seconds": round(readiness.elapsed_seconds, 2),
        },
        tone="success",
    )


def _show_readiness_probe(probe: DatabaseReadinessProbe) -> None:
    observed = ", ".join(probe.observed_revisions) or "none"
    error = f"; error: {probe.last_error_type}" if probe.last_error_type else ""
    console.print(
        f"[{duration(probe.elapsed_seconds)}] database revisions: {observed}{error}",
        highlight=False,
        markup=False,
    )


def _schema_payload(
    inspection: DatabaseSchemaInspection,
) -> dict[str, str | list[str] | None]:
    return {
        "state": inspection.state.value,
        "current_revision": inspection.current_revision,
        "observed_revisions": list(inspection.observed_revisions),
        "target_revision": inspection.target_revision,
    }


def _show_schema(
    ctx: typer.Context,
    inspection: DatabaseSchemaInspection,
    *,
    title: str,
) -> None:
    payload = _schema_payload(inspection)
    emit_result(
        ctx,
        payload=payload,
        title=title,
        fields={
            "state": inspection.state.value,
            "current": inspection.current_revision,
            "target": inspection.target_revision,
        },
        tone="success" if inspection.current_revision == inspection.target_revision else "warning",
    )


__all__ = ["database_app"]
