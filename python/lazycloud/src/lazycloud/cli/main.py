from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from typing import Annotated, Any

import typer

from lazycloud._shared.client_version import observe_client_versions, release_is_newer
from lazycloud._terminal.streams import error_console, json_output_active, set_json_output
from lazycloud.cli.apps import app_app
from lazycloud.cli.artifacts import artifact_app
from lazycloud.cli.components.errors import (
    CLIENT_ERROR_POLICY,
)
from lazycloud.cli.components.output import CliContextState
from lazycloud.cli.components.runner import run_cli
from lazycloud.cli.devbox import devbox_app
from lazycloud.cli.development import dev
from lazycloud.cli.disks import disk_app
from lazycloud.cli.domains import domain_app
from lazycloud.cli.examples import example_app
from lazycloud.cli.execution import deploy, deployment_app, run, shell
from lazycloud.cli.identity import login, profile_app, token_app
from lazycloud.cli.logs import logs
from lazycloud.cli.requests import requests_app
from lazycloud.cli.resources import (
    cloud_app,
    compute_app,
    container_app,
    machine_app,
)
from lazycloud.cli.secrets import secret_app
from lazycloud.cli.serve import serve
from lazycloud.cli.ssh import ssh, ssh_cert, ssh_config, ssh_proxy
from lazycloud.cli.tasks import task_app
from lazycloud.cli.update import update
from lazycloud.cli.volumes import volume_app, volume_cp, volume_ls, volume_mv, volume_rm
from lazycloud.cli.webhooks import webhook_app
from lazycloud.cli.workspaces import workspace_app
from lazycloud.self_update import installed_version

_GLOBAL_FLAGS = ("--json", "--debug")


_PASSTHROUGH = {"ignore_unknown_options": True, "allow_extra_args": True}
_COMMANDS: tuple[tuple[str, Callable[..., Any], dict[str, Any]], ...] = (
    ("deploy", deploy, {"help": "Deploy a handler or apps from Python files and modules."}),
    ("run", run, {"help": "Run a handler locally or remotely.", "context_settings": _PASSTHROUGH}),
    (
        "shell",
        shell,
        {
            "help": "Open an interactive shell for a handler or container.",
            "context_settings": _PASSTHROUGH,
        },
    ),
    (
        "ssh",
        ssh,
        {
            "help": "Open an SSH session to a devbox or a pod that serves SSH.",
            "context_settings": _PASSTHROUGH,
        },
    ),
    (
        "ssh-proxy",
        ssh_proxy,
        {"help": "Carry an SSH connection to a pod over stdin and stdout.", "hidden": True},
    ),
    (
        "ssh-config",
        ssh_config,
        {"help": "Write SSH config so ssh and editors reach devboxes and pods by name."},
    ),
    (
        "ssh-cert",
        ssh_cert,
        {"help": "Refresh the SSH certificate for the workspace.", "hidden": True},
    ),
    ("serve", serve, {"help": "Serve an app locally."}),
    ("login", login, {"help": "Authenticate the CLI profile."}),
    ("dev", dev, {"help": "Run local development helpers."}),
    ("logs", logs, {"help": "Inspect task and container logs."}),
    ("update", update, {"help": "Upgrade the installed lazycloud client."}),
    ("ls", volume_ls, {"help": "List files in a volume."}),
    ("cp", volume_cp, {"help": "Copy files to or from a volume."}),
    ("rm", volume_rm, {"help": "Remove files from a volume."}),
    ("mv", volume_mv, {"help": "Move or rename files in a volume."}),
)
_GROUPS: tuple[tuple[str, typer.Typer], ...] = (
    ("devbox", devbox_app),
    ("profile", profile_app),
    ("token", token_app),
    ("task", task_app),
    ("deployment", deployment_app),
    ("container", container_app),
    ("machine", machine_app),
    ("secret", secret_app),
    ("domain", domain_app),
    ("webhook", webhook_app),
    ("requests", requests_app),
    ("volume", volume_app),
    ("disk", disk_app),
    ("artifact", artifact_app),
    ("example", example_app),
    ("workspace", workspace_app),
    ("cloud", cloud_app),
    ("compute", compute_app),
    ("app", app_app),
)


def build_public_cli() -> typer.Typer:
    """Build an isolated public command tree."""
    application = typer.Typer(
        help="Deploy, run, and manage workloads on lazycloud.",
        context_settings={"help_option_names": ["-h", "--help"]},
        no_args_is_help=True,
        rich_markup_mode="rich",
    )
    application.callback()(_public_cli_callback)
    for name, command, options in _COMMANDS:
        application.command(name, **options)(command)
    for name, group in _GROUPS:
        application.add_typer(deepcopy(group), name=name)
    return application


def start(args: list[str] | None = None, prog_name: str | None = None) -> None:
    effective_args = normalize_global_flags(list(sys.argv[1:] if args is None else args))
    run_cli(
        build_public_cli(),
        args=effective_args,
        prog_name=prog_name,
        policy=CLIENT_ERROR_POLICY,
    )


def normalize_global_flags(args: list[str]) -> list[str]:
    promoted: list[str] = []
    remaining: list[str] = []
    after_command_separator = False
    for arg in args:
        if arg == "--":
            after_command_separator = True
            remaining.append(arg)
            continue
        if not after_command_separator and arg in _GLOBAL_FLAGS:
            if arg not in promoted:
                promoted.append(arg)
            continue
        remaining.append(arg)
    return [*promoted, *remaining]


def _public_cli_callback(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print structured JSON output."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Show full tracebacks for execution errors."),
    ] = False,
) -> None:
    ctx.obj = CliContextState(json=json_output, debug=debug)
    previous_json_output = json_output_active()
    set_json_output(json_output)
    ctx.call_on_close(lambda: set_json_output(previous_json_output))
    current = installed_version()
    notified = False

    def notify_update(recommended: str) -> None:
        nonlocal notified
        if not notified and release_is_newer(recommended, current):
            notified = True
            error_console.print(
                f"lazycloud {current} is out of date; this server recommends {recommended}. "
                "Run `lazycloud update` to upgrade.",
                markup=False,
                soft_wrap=True,
            )

    ctx.with_resource(observe_client_versions(notify_update))


if __name__ == "__main__":
    start()
