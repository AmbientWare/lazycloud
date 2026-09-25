from __future__ import annotations

from typing import Annotated

import typer

from lazycloud.agent_harness import AgentHarness
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import json_output_enabled, write_stream
from lazycloud.cli.ssh import AppOption, WorkspaceOption, ssh_connection
from lazycloud.session.agent_login import login_agent

devbox_app = typer.Typer(help="Connect coding agents to their accounts inside a devbox.")


@devbox_app.command("login")
def login(
    ctx: typer.Context,
    pod: Annotated[str, typer.Argument(help="Devbox or SSH-enabled pod to log into.")],
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
    with ssh_connection(pod, app=app, workspace=workspace) as (access, host):
        status = login_agent(access.paths.config, host.alias, harness)
    raise typer.Exit(status)
