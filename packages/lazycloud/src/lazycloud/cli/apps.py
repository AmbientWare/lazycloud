from __future__ import annotations

from typing import Annotated

import typer

from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.control import resource_client

app_app = typer.Typer(help="Manage deployed applications.")


@app_app.command("list")
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
            item.id,
        ]
        for item in response.data
    ]
    console.print(table("Apps", ["name", "state", "version", "public", "id"], rows))


@app_app.command("show")
def app_show(
    ctx: typer.Context,
    app_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).app(app_id)
    print_payload(ctx, response.model_dump(mode="json"), title="App")


@app_app.command("pause")
def app_pause(
    ctx: typer.Context,
    app_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).pause_app(app_id)
    print_payload(ctx, response.model_dump(mode="json"), title="App paused", tone="success")


@app_app.command("resume")
def app_resume(
    ctx: typer.Context,
    app_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).resume_app(app_id)
    print_payload(ctx, response.model_dump(mode="json"), title="App resumed", tone="success")


@app_app.command("delete")
def app_delete(
    ctx: typer.Context,
    app_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    resource_client(workspace=workspace).delete_app(app_id)
    print_payload(
        ctx,
        {"app_id": app_id, "deleted": True},
        title="App deleted",
        tone="success",
    )


__all__ = ["app_app"]
