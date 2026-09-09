from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from billing.economics import (
    EconomicsPeriod,
    EconomicsService,
    EconomicsStatement,
    OperationalStatement,
)
from lazycloud.cli.components.results import emit_result

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

economics_app = typer.Typer(
    help="Reconcile usage revenue, supplier statements and operating costs."
)


@economics_app.command("report")
def report(
    ctx: typer.Context,
    started_at: Annotated[str, typer.Option("--from", help="Period start with timezone.")],
    ended_at: Annotated[str, typer.Option("--to", help="Exclusive period end with timezone.")],
    statement: Annotated[
        Path | None, typer.Option("--statement", help="Actual financial statement JSON.")
    ] = None,
    operations: Annotated[
        Path | None, typer.Option("--operations", help="Observed fleet totals JSON.")
    ] = None,
) -> None:
    """Read one installation's ledger and reconcile an exact accounting period.

    Amounts use USD nanodollars. Quotes and current node snapshots cannot establish
    realized supplier cost. Missing evidence leaves margins unset. Exit 2 means
    incomplete evidence; exit 1 means contribution or operating loss.
    """
    period = EconomicsPeriod.model_validate({"started_at": started_at, "ended_at": ended_at})
    financial = (
        EconomicsStatement.model_validate_json(statement.read_bytes()) if statement else None
    )
    operational = (
        OperationalStatement.model_validate_json(operations.read_bytes()) if operations else None
    )
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    try:
        result = EconomicsService(database).report(
            period, statement=financial, operations=operational
        )
    finally:
        database.dispose()
    emit_result(
        ctx,
        payload=result.model_dump(mode="json"),
        title="Economics report",
        fields={
            "status": result.status.value,
            "gross usage nanos": result.gross_usage_nanos,
            "net revenue nanos": result.net_revenue_nanos,
            "contribution nanos": result.contribution_nanos,
            "operating result nanos": result.operating_result_nanos,
            "missing statements": [component.value for component in result.missing_components],
            "reconciliation gaps": "; ".join(result.reconciliation_gaps),
            "operational gaps": "; ".join(result.occupancy.gaps),
            "storage access gaps": "; ".join(result.storage_access.gaps),
        },
        tone="warning" if result.exit_code else "success",
    )
    if result.exit_code:
        raise typer.Exit(result.exit_code)
