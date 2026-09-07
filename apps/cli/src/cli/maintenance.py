from __future__ import annotations

from dataclasses import dataclass

import typer
from lazycloud.cli.components.results import emit_result
from observability.log_retention import LogRetentionService
from pydantic import JsonValue

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

maintenance_app = typer.Typer(help="Run bounded platform maintenance jobs.")


@dataclass(slots=True)
class _LogRetentionContext:
    database: DatabaseClient


@maintenance_app.command("prune-logs")
def prune_logs(ctx: typer.Context) -> None:
    """Permanently delete expired task logs using each workspace owner's plan."""
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            application_name=DatabaseApplicationName.Admin,
            pool_size=1,
            max_overflow=0,
            statement_timeout_ms=5_000,
        )
    )
    try:
        result = LogRetentionService(_LogRetentionContext(database)).run()
    finally:
        database.dispose()
    payload: dict[str, JsonValue] = {
        "deleted": result.deleted,
        "batches": result.batches,
        "budget_exhausted": result.budget_exhausted,
    }
    emit_result(ctx, payload=payload, title="Log retention", fields=payload)
