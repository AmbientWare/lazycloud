from __future__ import annotations

from typing import Annotated

import typer
from shared.deployments import PodRole
from shared.ssh import SSH_HOST_LIST_LIMIT
from typer._click import Command, Context
from typer.core import TyperGroup
from typer.main import get_group

from lazycloud._terminal.cards import result_card
from lazycloud.agent_harness import AgentHarness
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import (
    emit,
    json_output_enabled,
    table,
    write_stream,
)
from lazycloud.cli.control import resource_client, ssh_client
from lazycloud.cli.ssh import AppOption, WorkspaceOption, ssh_connection
from lazycloud.session.agent_login import login_agent
from lazycloud.session.ssh import run_ssh
from lazycloud.terminal import humanize_bytes

_named_devbox = typer.Typer(help="Log in, connect, or inspect a devbox by name.")


class DevboxGroup(TyperGroup):
    def list_commands(self, ctx: Context) -> list[str]:
        return [*super().list_commands(ctx), "NAME"]

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is None:
            command = get_group(_named_devbox)
            command.name = cmd_name
        return command


devbox_app = typer.Typer(cls=DevboxGroup, help="List devboxes or run devbox NAME COMMAND.")


def _name(ctx: typer.Context) -> str:
    if ctx.parent is None or ctx.parent.info_name is None:
        raise ClientError("A devbox name is required")
    return ctx.parent.info_name


@devbox_app.command("list")
def list_devboxes(
    ctx: typer.Context,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
    limit: Annotated[
        int, typer.Option(min=1, max=SSH_HOST_LIST_LIMIT, help="Maximum boxes per page.")
    ] = SSH_HOST_LIST_LIMIT,
    cursor: Annotated[
        str, typer.Option(help="Continue from the previous page's next cursor.")
    ] = "",
) -> None:
    """List deployed devboxes without starting them."""
    page = ssh_client(workspace=workspace).hosts(
        app=app, role=PodRole.Devbox, limit=limit, cursor=cursor
    )
    emit(
        ctx,
        payload=page,
        view=table(
            "devboxes",
            ["name", "app"],
            [[host.pod, host.app] for host in page.data],
        ),
    )
    if page.next and not json_output_enabled(ctx):
        write_stream(f"Next page: --cursor {page.next}\n")


@_named_devbox.command("login")
def login(
    ctx: typer.Context,
    harness: Annotated[AgentHarness, typer.Argument(help="Installed coding agent.")],
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Run native browser login with private, temporary callback forwarding."""
    if json_output_enabled(ctx):
        raise ClientError("Agent login is interactive and does not support --json")
    if harness is AgentHarness.Pi:
        write_stream(
            "In Pi, run /login and choose your provider's browser login. Exit Pi when done.\n"
        )
    elif harness is AgentHarness.OpenCode:
        write_stream("Choose your provider and its browser login method.\n")
    with ssh_connection(_name(ctx), app=app, workspace=workspace, role=PodRole.Devbox) as (
        access,
        host,
    ):
        status = login_agent(access.paths.config, host.alias, harness)
    raise typer.Exit(status)


@_named_devbox.command("ssh", context_settings={"allow_extra_args": True})
def ssh(
    ctx: typer.Context,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Open a shell. Arguments after -- go to ssh."""
    if json_output_enabled(ctx):
        raise ClientError("SSH does not support --json")
    with ssh_connection(_name(ctx), app=app, workspace=workspace, role=PodRole.Devbox) as (
        access,
        host,
    ):
        result = run_ssh(access.paths.config, host.alias, list(ctx.args))
    raise typer.Exit(result)


@_named_devbox.command("status")
def status(
    ctx: typer.Context,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Show live state and resources without starting the devbox."""
    name = _name(ctx)
    matches = ssh_client(workspace=workspace).hosts(app=app, pod=name, role=PodRole.Devbox, limit=2)
    if not matches.data:
        raise ClientError(f"devbox not found: {name}")
    if len(matches.data) > 1 or matches.next:
        raise ClientError(f"Several apps have a devbox named {name}; select one with --app")
    host = matches.data[0]
    detail = resource_client(workspace=workspace).deployment(host.deployment_id)
    box = detail.devbox
    if box is None:
        raise ClientError(f"{name!r} is no longer a devbox; check devbox list")
    resources = detail.spec.resources.model_dump(mode="json")
    emit(
        ctx,
        payload=detail,
        view=result_card(
            {
                "phase": box.phase.value,
                "cpu": resources["cpu"],
                "memory": resources["memory"],
                "disk": humanize_bytes(box.disk.size_bytes) if box.disk else resources["disk"],
                "connections": box.open_connections,
            },
            title=f"{matches.workspace}/{host.app}/{name}",
            message=box.phase_reason,
        ),
    )
