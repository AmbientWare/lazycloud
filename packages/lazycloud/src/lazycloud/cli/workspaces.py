from __future__ import annotations

from typing import Annotated

import typer

from lazycloud.cli.components import theme
from lazycloud.cli.components.formatting import timestamp
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.clients.workspace import WorkspaceControlClient
from lazycloud.config import ClientProfile, get_profile, set_profile

workspace_app = typer.Typer(help="Manage the active workspace.")


def _client(profile: ClientProfile) -> WorkspaceControlClient:
    return WorkspaceControlClient.from_endpoint(
        profile.resolved_endpoint(),
        token=profile.token,
        workspace=profile.workspace,
    )


@workspace_app.command("show", help="Show the active workspace.")
def workspace_show(ctx: typer.Context) -> None:
    profile = get_profile()
    print_payload(ctx, _client(profile).current().model_dump(mode="json"), title="Workspace")


@workspace_app.command("rename", help="Rename the active workspace.")
def workspace_rename(ctx: typer.Context, name: str) -> None:
    profile = get_profile()
    workspace = _client(profile).rename(name)
    set_profile(
        profile.model_copy(update={"workspace": workspace.name}),
        activate=True,
        replace_legacy=True,
    )
    print_payload(
        ctx,
        workspace.model_dump(mode="json"),
        title="Workspace renamed",
        tone="success",
    )


@workspace_app.command("audit", help="List recent workspace changes.")
def workspace_audit(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 50,
    cursor: Annotated[str | None, typer.Option()] = None,
) -> None:
    profile = get_profile()
    response = _client(profile).audit(limit=limit, cursor=cursor)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [
        [
            timestamp(event.created_at),
            event.actor_name,
            event.summary,
        ]
        for event in response.data
    ]
    console.print(table("Workspace audit", ["time", "actor", "change"], rows))
    if response.next:
        console.print(theme.styled(f"Next cursor: {response.next}", theme.MUTED))


__all__ = ["workspace_app", "workspace_audit", "workspace_rename", "workspace_show"]
