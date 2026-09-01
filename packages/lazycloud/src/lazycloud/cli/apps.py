from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer

from lazycloud.cli.components.cards import notice_card, result_card
from lazycloud.cli.components.output import (
    console,
    emit,
    json_output_enabled,
    print_payload,
    table,
)
from lazycloud.cli.control import resource_client
from lazycloud.clients.resource.control import ResourceControlClient

app_app = typer.Typer(help="Manage deployed applications.")


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
    active_filter = True if active else False if inactive else None
    response = resource_client(workspace=workspace).list_apps(active=active_filter)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows: list[list[object]] = [
        [
            item.name,
            item.lifecycle_state.value,
            item.version,
            item.public,
        ]
        for item in response.data
    ]
    console.print(table("Apps", ["name", "state", "version", "public"], rows))


def resolve_app_id(value: str, *, client: ResourceControlClient) -> str:
    """Resolve the app name shown by the CLI while still accepting an exact id."""
    try:
        UUID(value)
    except ValueError:
        pass
    else:
        return value
    apps = client.list_apps()
    match = next((item for item in apps.data if item.name == value), None)
    if match is None:
        raise typer.BadParameter(f"no app named {value!r} in this workspace")
    return match.id


@app_app.command("show", help="Show one deployed application.")
def app_show(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App name or ID.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = resource_client(workspace=workspace)
    response = client.app(resolve_app_id(app, client=client))
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            "App",
            {
                "name": response.name,
                "state": response.lifecycle_state.value,
                "version": response.version,
                "public": response.public,
            },
        ),
    )


@app_app.command("pause", help="Pause an application's workloads.")
def app_pause(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App name or ID.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = resource_client(workspace=workspace)
    response = client.pause_app(resolve_app_id(app, client=client))
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            "App paused",
            f"Paused {response.name}.",
            tone="success",
        ),
    )


@app_app.command("resume", help="Resume a paused application.")
def app_resume(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App name or ID.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = resource_client(workspace=workspace)
    response = client.resume_app(resolve_app_id(app, client=client))
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            "App resumed",
            f"Resumed {response.name}.",
            tone="success",
        ),
    )


@app_app.command("delete", help="Delete a deployed application.")
def app_delete(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App name or ID.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = resource_client(workspace=workspace)
    app_id = resolve_app_id(app, client=client)
    client.delete_app(app_id)
    emit(
        ctx,
        payload={"app_id": app_id, "deleted": True},
        view=notice_card("App deleted", f"Deleted {app}.", tone="success"),
    )


__all__ = ["app_app", "resolve_app_id"]
