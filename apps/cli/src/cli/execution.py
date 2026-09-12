from __future__ import annotations

from typing import Annotated

import typer
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    json_output_enabled,
    parse_json_argument,
    print_events_table,
    print_payload,
)
from lazycloud.cli.handler_workflows import HandlerLoadError
from lazycloud.json_contracts import resource_payload, validate_json_value
from shared.http.observability import EventHistoryRequest

from cli.api_client import admin_api_client
from cli.workflow_options import (
    is_function_workload,
    is_json_callable,
    load_cli_handler,
)


def invoke(
    ctx: typer.Context,
    handler: str,
    args: Annotated[list[str] | None, typer.Argument()] = None,
) -> None:
    try:
        user_object = load_cli_handler(handler)
    except HandlerLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc
    parsed_args = [validate_json_value(parse_json_argument(item)) for item in args or []]
    if is_function_workload(user_object):
        response = user_object.remote(*parsed_args)
    elif is_json_callable(user_object):
        response = user_object(*parsed_args)
    else:
        raise typer.BadParameter("invoke requires a callable LazyCloud handler")
    print_payload(ctx, resource_payload(response))


def events(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
    resource_type: Annotated[str | None, typer.Option("--resource-type")] = None,
    resource_id: Annotated[str | None, typer.Option("--resource-id")] = None,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
) -> None:
    selected_workspace = current_workspace(workspace)
    result = admin_api_client(selected_workspace).events(
        EventHistoryRequest(
            workspace_id=selected_workspace,
            task_id=task_id,
            container_id=container_id,
            resource_type=resource_type,
            resource_id=resource_id,
            limit=limit,
            cursor=cursor,
        )
    )
    if json_output_enabled(ctx):
        print_payload(ctx, result.model_dump(mode="json"))
        return
    print_events_table("Events", list(result.data))


__all__ = ["events", "invoke"]
