from __future__ import annotations

import time
from typing import Annotated

import typer
from typer._click import Command, Context
from typer.core import TyperGroup
from typer.main import get_group

from lazycloud._shared.ssh import SSH_HOST_LIST_LIMIT
from lazycloud._terminal.cards import notice_card, result_card
from lazycloud.agent_harness import AgentHarness
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import (
    emit,
    json_output_enabled,
    table,
    write_stream,
)
from lazycloud.cli.components.progress import CONNECTING_POLL_SECONDS
from lazycloud.cli.control import workloads
from lazycloud.cli.ssh import AppOption, WorkspaceOption, cancel_devbox_start, ssh_connection
from lazycloud.clients.workloads import WorkloadsClient
from lazycloud.contracts.api import Devbox, DevboxPhase, DevboxState, PodRole, Resources, SshHost
from lazycloud.session.agent_login import login_agent
from lazycloud.session.ssh import run_ssh
from lazycloud.terminal import Terminal, humanize_bytes

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
    host = _devbox_host(client, name, app)
    box = client.devbox(host.app, host.pod)
    resources = box.resources
    if resources is None:
        raise ClientError(f"'{name}' is no longer a devbox; check devbox list")
    emit(
        ctx,
        payload=box.model_dump(mode="json"),
        view=result_card(
            {
                "phase": box.phase.value,
                "cpu": _cpus(resources),
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


@_named_devbox.command("start")
def start(
    ctx: typer.Context,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Start the devbox and wait until it runs. Ctrl-C while it starts stops it."""
    name = _name(ctx)
    client = workloads(workspace=workspace)
    host = _devbox_host(client, name, app)
    box = client.start_devbox(host.app, host.pod)
    try:
        box = _wait_running(client, host, box)
    except KeyboardInterrupt:
        cancel_devbox_start(client, host.app, host.pod)
        raise
    emit(
        ctx,
        payload=box.model_dump(mode="json"),
        view=notice_card(f"{name} is running; connect with `{box.ssh_command}`.", tone="success"),
    )


@_named_devbox.command("stop")
def stop(
    ctx: typer.Context,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Stop the devbox whatever it is doing, cancelling a start; its disk is saved."""
    name = _name(ctx)
    client = workloads(workspace=workspace)
    host = _devbox_host(client, name, app)
    box = client.stop_devbox(host.app, host.pod)
    emit(
        ctx,
        payload=box.model_dump(mode="json"),
        view=notice_card(f"Stopped {name}.", tone="success"),
    )


def _devbox_host(client: WorkloadsClient, name: str, app: str | None) -> SshHost:
    matches = client.ssh_hosts(app=app, pod=name, role=PodRole.devbox, limit=2)
    if not matches.hosts:
        raise ClientError(f"devbox not found: {name}")
    if len(matches.hosts) > 1 or matches.next_cursor:
        raise ClientError(f"Several apps have a devbox named {name}; select one with --app")
    return matches.hosts[0]


def _wait_running(client: WorkloadsClient, host: SshHost, box: Devbox) -> Devbox:
    """Follow a started devbox until it runs; a start that fails or ends is an error.

    A failure from before the start still shows until the next container exists, so
    only a failed container other than that one counts.
    """
    earlier = box.failed_container_id if box.phase is DevboxPhase.failed else None
    with Terminal().step("Starting", host.pod) as step:
        while box.state is not DevboxState.running:
            if box.phase is DevboxPhase.failed and box.failed_container_id != earlier:
                raise ClientError(
                    f"{host.pod} failed to start: {box.phase_reason or 'see its start logs'}",
                    type="devbox_start_failed",
                    hint=f"It retries; `lazycloud devbox {host.pod} stop` stops it.",
                )
            if box.phase is DevboxPhase.stopped:
                raise ClientError(
                    f"{host.pod} stopped before it was running", type="devbox_stopped"
                )
            step.update(f"{host.pod} · {box.phase.value.replace('_', ' ')}")
            time.sleep(CONNECTING_POLL_SECONDS)
            box = client.devbox(host.app, host.pod)
    return box


def _cpus(resources: Resources) -> str:
    cpus = resources.cpu_millis / 1000
    return f"{int(cpus) if cpus.is_integer() else cpus} CPU"


def _mebibytes(value: int) -> str:
    return f"{value // 1024}Gi" if value % 1024 == 0 else f"{value}Mi"
