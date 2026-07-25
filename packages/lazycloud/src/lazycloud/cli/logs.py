from __future__ import annotations

from typing import Annotated

import typer
from shared.http.observability import LogObjectType, LogQueryRequest, LogRecord

from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import console, json_output_enabled, print_payload
from lazycloud.cli.control import observability_client


def logs(
    ctx: typer.Context,
    stub_id: Annotated[str | None, typer.Option("--stub-id")] = None,
    deployment_id: Annotated[str | None, typer.Option("--deployment-id")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", min=1, help="Display the last N lines."),
    ] = 250,
    show_timestamp: Annotated[
        bool,
        typer.Option("--show-timestamp", help="Include log timestamps."),
    ] = False,
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Follow new logs."),
    ] = False,
    max_events: Annotated[
        int,
        typer.Option("--max-events", min=0, help="Stop after N streamed log events."),
    ] = 0,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    object_id, object_type = _selected_log_target(
        stub_id=stub_id,
        deployment_id=deployment_id,
        task_id=task_id,
        container_id=container_id,
    )
    request = LogQueryRequest(
        workspace_id=current_workspace(workspace),
        object_id=object_id,
        object_type=object_type,
        limit=lines,
    )
    client = observability_client(workspace=workspace)
    if follow:
        for item in client.stream_logs(request, max_events=max_events):
            _print_log_item(ctx, item, show_timestamp=show_timestamp)
        return
    response = client.logs(request)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    records = [_log_record_line(item) for item in response.data]
    if not records:
        console.print("No logs found.")
        return
    for timestamp, message in records:
        line = f"[{timestamp}] {message}" if show_timestamp else message
        console.print(line, highlight=False, end="" if line.endswith("\n") else "\n")


def _selected_log_target(
    *,
    stub_id: str | None,
    deployment_id: str | None,
    task_id: str | None,
    container_id: str | None,
) -> tuple[str, LogObjectType]:
    selected = [
        (stub_id, LogObjectType.Stub),
        (deployment_id, LogObjectType.Deployment),
        (task_id, LogObjectType.Task),
        (container_id, LogObjectType.Container),
    ]
    present = [(object_id, object_type) for object_id, object_type in selected if object_id]
    if len(present) != 1:
        msg = "supply exactly one of --stub-id, --deployment-id, --task-id, or --container-id"
        raise typer.BadParameter(msg)
    object_id, object_type = present[0]
    return object_id, object_type


def _print_log_item(
    ctx: typer.Context,
    item: LogRecord,
    *,
    show_timestamp: bool,
) -> None:
    if json_output_enabled(ctx):
        print_payload(ctx, item.model_dump(mode="json"))
        return
    timestamp, message = _log_record_line(item)
    line = f"[{timestamp}] {message}" if show_timestamp else message
    console.print(line, highlight=False, end="" if line.endswith("\n") else "\n")


def _log_record_line(item: LogRecord) -> tuple[str, str]:
    return item.timestamp.isoformat(), item.message


__all__ = ["logs"]
