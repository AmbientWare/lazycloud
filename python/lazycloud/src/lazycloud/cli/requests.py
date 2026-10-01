from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import typer

from lazycloud._terminal.streams import console
from lazycloud.cli.components.output import json_output_enabled, print_payload, table
from lazycloud.cli.control import api_session
from lazycloud.clients.endpoints import http_request_logs, list_http_requests

requests_app = typer.Typer(help="See the requests endpoints and ASGI apps served.")


@requests_app.command("list", help="List an app's recent requests, newest first.")
def requests_list(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="The app, or APP:NAME for one endpoint or ASGI app.")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = api_session(workspace=workspace)
    app_name, _, name = app.partition(":")
    requests = list_http_requests(client, selected, app_name, name=name or None, limit=limit)
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in requests])
        return
    rows: list[list[Any]] = [
        [
            str(item.id),
            item.name,
            item.method,
            item.path,
            item.status,
            f"{item.duration_ms} ms",
            item.started_at.isoformat(timespec="seconds"),
        ]
        for item in requests
    ]
    console.print(table("Requests", ["id", "name", "method", "path", "status", "took", "at"], rows))


@requests_app.command("logs", help="Print what the workload wrote while serving a request.")
def requests_logs(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="The request's X-Request-Id.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = api_session(workspace=workspace)
    try:
        request = UUID(request_id)
    except ValueError as exc:
        raise typer.BadParameter("a request id is a UUID") from exc
    entries = http_request_logs(client, selected, request)
    if json_output_enabled(ctx):
        print_payload(ctx, [entry.model_dump(mode="json") for entry in entries])
        return
    for entry in entries:
        console.print(entry.data, markup=False, highlight=False)
