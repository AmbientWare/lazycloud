from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer
from pydantic import JsonValue

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import timestamp
from lazycloud._terminal.streams import console
from lazycloud.cli.app_export import app_export
from lazycloud.cli.components.output import emit, json_output_enabled, print_payload, table
from lazycloud.cli.control import api_session
from lazycloud.clients.api import ApiClient
from lazycloud.contracts.api import App, LiveAppState

app_app = typer.Typer(help="Manage deployed applications.")
app_app.command("export", help="Generate a typed Python package for an app.")(app_export)

_APP_ARGUMENT = typer.Argument(help="App name or ID.")


@app_app.command("list", help="List deployed applications.")
def app_list(
    ctx: typer.Context,
    active: Annotated[bool, typer.Option("--active")] = False,
    inactive: Annotated[bool, typer.Option("--inactive")] = False,
    all_apps: Annotated[bool, typer.Option("--all")] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    if sum((active, inactive, all_apps)) > 1:
        raise typer.BadParameter("choose only one of --active, --inactive, or --all")
    state = LiveAppState.active if active else LiveAppState.paused if inactive else None
    client, selected_workspace = api_session(workspace=workspace)
    response = client.list_apps(selected_workspace, state=state)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows: list[list[object]] = [
        [item.name, item.state.value, item.workloads, timestamp(item.created_at)]
        for item in response.apps
    ]
    console.print(table("Apps", ["name", "state", "workloads", "created"], rows))


@app_app.command("show", help="Show one deployed application.")
def app_show(
    ctx: typer.Context,
    app: Annotated[str, _APP_ARGUMENT],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    response = client.get_app(selected_workspace, app)
    emit(ctx, payload=response.model_dump(mode="json"), view=result_card(_app_summary(response)))


@app_app.command("pause", help="Pause an application's workloads.")
def app_pause(
    ctx: typer.Context,
    app: Annotated[str, _APP_ARGUMENT],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    response = client.pause_app(selected_workspace, app)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(f"Paused {response.name}.", tone="success"),
    )


@app_app.command("resume", help="Resume a paused application.")
def app_resume(
    ctx: typer.Context,
    app: Annotated[str, _APP_ARGUMENT],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    response = client.resume_app(selected_workspace, app)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(f"Resumed {response.name}.", tone="success"),
    )


@app_app.command("delete", help="Delete a deployed application.")
def app_delete(
    ctx: typer.Context,
    app: Annotated[str, _APP_ARGUMENT],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    response = client.delete_app(selected_workspace, app)
    emit(
        ctx,
        payload={"app_id": str(response.id), "deleted": True},
        view=notice_card(f"Deleted {app}.", tone="success"),
    )


def app_name(client: ApiClient, workspace: str, value: str) -> str:
    """An app filter as the name the API lists by, accepting an id too."""
    try:
        UUID(value)
    except ValueError:
        return value
    return client.get_app(workspace, value).name


def _app_summary(app: App) -> dict[str, JsonValue]:
    return {
        "name": app.name,
        "state": app.state.value,
        "workloads": app.workloads,
        "created": timestamp(app.created_at),
    }


__all__ = ["app_app", "app_name"]
