from __future__ import annotations

from typing import Annotated

import typer
from shared.api import PodRole, Resources
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
from lazycloud.cli.control import workloads
from lazycloud.cli.ssh import AppOption, WorkspaceOption, ssh_connection
from lazycloud.session.agent_login import login_agent
from lazycloud.session.ssh import run_ssh
from lazycloud.terminal import humanize_bytes

_named_devbox = typer.Typer(help="Log in, connect, or inspect a devbox by name.")


class DevboxGroup(TyperGroup):
    def resolve_command(
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Command | None, list[str]]:
        # A box may itself be named "list".
        if len(args) > 1 and args[0] in self.commands:
            named = get_group(_named_devbox)
            if args[1] in named.commands:
                named.name = args[0]
                return args[0], named, args[1:]
        return super().resolve_command(ctx, args)

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
    page = workloads(workspace=workspace).ssh_hosts(
        app=app, role=PodRole.devbox, limit=limit, cursor=cursor or None
    )
    emit(
        ctx,
        payload=page.model_dump(mode="json"),
        view=table(
            "devboxes",
            ["name", "app"],
            [[host.pod, host.app] for host in page.hosts],
        ),
    )
    if page.next_cursor and not json_output_enabled(ctx):
        write_stream(f"Next page: --cursor {page.next_cursor}\n")


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
    with ssh_connection(_name(ctx), app=app, workspace=workspace, role=PodRole.devbox) as (
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
    with ssh_connection(_name(ctx), app=app, workspace=workspace, role=PodRole.devbox) as (
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
    client = workloads(workspace=workspace)
    matches = client.ssh_hosts(app=app, pod=name, role=PodRole.devbox, limit=2)
    if not matches.hosts:
        raise ClientError(f"devbox not found: {name}")
    if len(matches.hosts) > 1 or matches.next_cursor:
        raise ClientError(f"Several apps have a devbox named {name}; select one with --app")
    host = matches.hosts[0]
    box = client.devbox(host.deployment_id)
    resources = client.api.get_function(
        client.workspace, host.app, name
    ).active_release.spec.resources
    emit(
        ctx,
        payload=box.model_dump(mode="json"),
        view=result_card(
            {
                "phase": box.phase.value,
                "cpu": _cores(resources),
                "memory": _mebibytes(resources.memory_mib),
                "disk": (
                    humanize_bytes(box.disk.size_bytes)
                    if box.disk
                    else _mebibytes(resources.disk_mib)
                    if resources.disk_mib
                    else None
                ),
                "connections": box.open_connections,
            },
            title=f"{client.workspace}/{host.app}/{name}",
            message=box.phase_reason or "",
        ),
    )


def _cores(resources: Resources) -> float | int:
    cores = resources.cpu_millis / 1000
    return int(cores) if cores.is_integer() else cores


def _mebibytes(value: int) -> str:
    return f"{value // 1024}Gi" if value % 1024 == 0 else f"{value}Mi"
