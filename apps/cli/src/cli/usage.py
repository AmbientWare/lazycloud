from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from shared.usage import UsageMetric

from cli.api_client import admin_api_client

usage_app = typer.Typer(help="Inspect usage records and billing summaries.")


@usage_app.command("list")
def usage_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    metric: Annotated[str | None, typer.Option("--metric")] = None,
    resource_type: Annotated[str | None, typer.Option("--resource-type")] = None,
    resource_id: Annotated[str | None, typer.Option("--resource-id")] = None,
    start: Annotated[str | None, typer.Option("--start")] = None,
    end: Annotated[str | None, typer.Option("--end")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
) -> None:
    metric_filter = _metric(metric)
    page = admin_api_client(workspace).list_usage_records(
        metric=metric_filter,
        resource_type=resource_type,
        resource_id=resource_id,
        start=_parsed_time(start),
        end=_parsed_time(end),
        limit=limit,
        cursor=cursor,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, page.model_dump(mode="json"))
        return
    rows = [
        [
            record.created_at.isoformat(),
            record.workspace_id,
            record.metric.value,
            str(record.quantity),
            record.unit.value,
            record.resource_type,
            record.resource_id,
        ]
        for record in page.data
    ]
    console.print(
        table(
            "Usage Records",
            ["time", "workspace", "metric", "quantity", "unit", "type", "resource"],
            rows,
        )
    )
    if page.next:
        console.print(f"Next cursor: {page.next}")


@usage_app.command("summary")
def usage_summary(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    metric: Annotated[str | None, typer.Option("--metric")] = None,
    resource_type: Annotated[str | None, typer.Option("--resource-type")] = None,
    resource_id: Annotated[str | None, typer.Option("--resource-id")] = None,
    start: Annotated[str | None, typer.Option("--start")] = None,
    end: Annotated[str | None, typer.Option("--end")] = None,
) -> None:
    response = admin_api_client(workspace).usage_summary(
        metric=_metric(metric),
        resource_type=resource_type,
        resource_id=resource_id,
        start=_parsed_time(start),
        end=_parsed_time(end),
    )
    rows = response.rows
    payload = [row.model_dump(mode="json") for row in rows]
    if json_output_enabled(ctx):
        print_payload(ctx, payload)
        return
    console.print(
        table(
            "Usage Summary",
            ["workspace", "metric", "quantity", "unit"],
            [
                [row.workspace_id, row.metric.value, str(row.quantity), row.unit.value]
                for row in rows
            ],
        )
    )


def _metric(value: str | None) -> UsageMetric | None:
    if value is None:
        return None
    try:
        return UsageMetric(value)
    except ValueError as exc:
        values = ", ".join(item.value for item in UsageMetric)
        msg = f"metric must be one of: {values}"
        raise typer.BadParameter(msg) from exc


def _parsed_time(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()


__all__ = ["usage_app"]
