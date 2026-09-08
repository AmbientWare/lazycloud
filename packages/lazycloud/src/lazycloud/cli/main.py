from __future__ import annotations

import sys
from collections.abc import Sequence
from copy import deepcopy
from typing import Annotated, Protocol, TypeVar

import typer
from shared.client_version import observe_client_versions, release_is_newer

from lazycloud.cli.apps import app_app
from lazycloud.cli.artifacts import artifact_app
from lazycloud.cli.client import client_app
from lazycloud.cli.components.errors import (
    CLIENT_ERROR_POLICY,
)
from lazycloud.cli.components.output import (
    CliContextState,
    error_console,
    json_output_active,
    set_json_output,
)
from lazycloud.cli.components.runner import run_cli
from lazycloud.cli.development import dev
from lazycloud.cli.domains import domain_app
from lazycloud.cli.examples import create_app, example_app, quickstart
from lazycloud.cli.execution import deploy, deployment_app, run, shell
from lazycloud.cli.identity import login, profile_app, token_app
from lazycloud.cli.logs import logs
from lazycloud.cli.resources import (
    cloud_app,
    compute_app,
    container_app,
    machine_app,
    task_app,
)
from lazycloud.cli.secrets import secret_app
from lazycloud.cli.serve import serve
from lazycloud.cli.update import update
from lazycloud.cli.volumes import volume_app, volume_cp, volume_ls, volume_mv, volume_rm
from lazycloud.cli.workspaces import workspace_app
from lazycloud.self_update import installed_version

_GLOBAL_FLAGS = ("--json", "--debug")
_RegistryValue = TypeVar("_RegistryValue")


class PublicCliExtension(Protocol):
    """Register commands on one freshly built public CLI."""

    def __call__(self, registry: PublicCliRegistry, /) -> None: ...


class CliRootRegistrar(Protocol):
    def __call__(self, application: typer.Typer, /) -> None: ...


class CliGroupExtension(Protocol):
    def __call__(self, group: typer.Typer, /) -> None: ...


class PublicCliRegistry:
    """Fresh named command and group registry for one CLI build."""

    def __init__(self, application: typer.Typer) -> None:
        self._application = application
        self._root_commands: dict[str, CliRootRegistrar] = {}
        self._groups: dict[str, typer.Typer] = {}

    def add_root_command(self, name: str, registrar: CliRootRegistrar) -> None:
        if name in self._root_commands:
            raise ValueError(f"root command {name!r} is already registered")
        self._root_commands[name] = registrar

    def remove_root_command(self, name: str) -> None:
        if name not in self._root_commands:
            raise ValueError(f"root command {name!r} is not registered")
        del self._root_commands[name]

    def add_group(self, name: str, template: typer.Typer) -> None:
        if name in self._groups:
            raise ValueError(f"command group {name!r} is already registered")
        self._groups[name] = deepcopy(template)

    def replace_group(self, name: str, template: typer.Typer) -> None:
        if name not in self._groups:
            raise ValueError(f"command group {name!r} is not registered")
        self._groups[name] = deepcopy(template)

    def extend_group(self, name: str, extension: CliGroupExtension) -> None:
        try:
            group = self._groups[name]
        except KeyError as exc:
            raise ValueError(f"command group {name!r} is not registered") from exc
        extension(group)

    def order_root_commands(self, names: Sequence[str]) -> None:
        self._root_commands = _ordered_registry(
            self._root_commands,
            names,
            label="root commands",
        )

    def order_groups(self, names: Sequence[str]) -> None:
        self._groups = _ordered_registry(self._groups, names, label="command groups")

    def compose(self) -> None:
        for registrar in self._root_commands.values():
            registrar(self._application)
        for name, group in self._groups.items():
            self._application.add_typer(group, name=name)


def _ordered_registry(
    registry: dict[str, _RegistryValue],
    names: Sequence[str],
    *,
    label: str,
) -> dict[str, _RegistryValue]:
    ordered_names = tuple(names)
    if len(set(ordered_names)) != len(ordered_names):
        raise ValueError(f"{label} order contains duplicate names")
    if set(ordered_names) != set(registry):
        missing = sorted(set(registry) - set(ordered_names))
        unknown = sorted(set(ordered_names) - set(registry))
        raise ValueError(f"{label} order mismatch: missing={missing}, unknown={unknown}")
    return {name: registry[name] for name in ordered_names}


def build_public_cli(
    extensions: Sequence[PublicCliExtension] = (),
    *,
    help: str = "Deploy, run, and manage workloads on lazycloud.",
) -> typer.Typer:
    """Build an isolated public command tree and apply this build's extensions."""
    application = typer.Typer(
        help=help,
        context_settings={"help_option_names": ["-h", "--help"]},
        no_args_is_help=True,
        rich_markup_mode="rich",
    )
    application.callback()(_public_cli_callback)
    registry = PublicCliRegistry(application)
    _register_public_commands(registry)
    _register_public_groups(registry)
    for extension in extensions:
        extension(registry)
    registry.compose()
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


def _register_public_commands(registry: PublicCliRegistry) -> None:
    registry.add_root_command("deploy", _register_deploy)
    registry.add_root_command("run", _register_run)
    registry.add_root_command("shell", _register_shell)
    registry.add_root_command("serve", _register_serve)
    registry.add_root_command("login", _register_login)
    registry.add_root_command("dev", _register_dev)
    registry.add_root_command("logs", _register_logs)
    registry.add_root_command("quickstart", _register_quickstart)
    registry.add_root_command("create-app", _register_create_app)
    registry.add_root_command("update", _register_update)
    registry.add_root_command("ls", _register_volume_ls)
    registry.add_root_command("cp", _register_volume_cp)
    registry.add_root_command("rm", _register_volume_rm)
    registry.add_root_command("mv", _register_volume_mv)


def _register_deploy(application: typer.Typer) -> None:
    application.command("deploy", help="Deploy a handler, an app, or the single app in a file.")(
        deploy
    )


def _register_run(application: typer.Typer) -> None:
    application.command(
        "run",
        help="Run a handler locally or remotely.",
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )(run)


def _register_shell(application: typer.Typer) -> None:
    application.command(
        "shell",
        help="Open an interactive shell for a handler or container.",
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )(shell)


def _register_serve(application: typer.Typer) -> None:
    application.command("serve", help="Serve an app locally.")(serve)


def _register_login(application: typer.Typer) -> None:
    application.command("login", help="Authenticate the CLI profile.")(login)


def _register_update(application: typer.Typer) -> None:
    application.command("update", help="Upgrade the installed lazycloud client.")(update)


def _register_dev(application: typer.Typer) -> None:
    application.command("dev", help="Run local development helpers.")(dev)


def _register_logs(application: typer.Typer) -> None:
    application.command("logs", help="Inspect task and container logs.")(logs)


def _register_quickstart(application: typer.Typer) -> None:
    application.command("quickstart", help="Write a starter app file.")(quickstart)


def _register_create_app(application: typer.Typer) -> None:
    application.command("create-app", help="Create a new app scaffold.")(create_app)


def _register_volume_ls(application: typer.Typer) -> None:
    application.command("ls", help="List files in a volume.")(volume_ls)


def _register_volume_cp(application: typer.Typer) -> None:
    application.command("cp", help="Copy files to or from a volume.")(volume_cp)


def _register_volume_rm(application: typer.Typer) -> None:
    application.command("rm", help="Remove files from a volume.")(volume_rm)


def _register_volume_mv(application: typer.Typer) -> None:
    application.command("mv", help="Move or rename files in a volume.")(volume_mv)


def _register_public_groups(registry: PublicCliRegistry) -> None:
    registry.add_group("profile", profile_app)
    registry.add_group("token", token_app)
    registry.add_group("task", task_app)
    registry.add_group("deployment", deployment_app)
    registry.add_group("container", container_app)
    registry.add_group("machine", machine_app)
    registry.add_group("secret", secret_app)
    registry.add_group("domain", domain_app)
    registry.add_group("volume", volume_app)
    registry.add_group("artifact", artifact_app)
    registry.add_group("example", example_app)
    registry.add_group("workspace", workspace_app)
    registry.add_group("cloud", cloud_app)
    registry.add_group("compute", compute_app)
    registry.add_group("app", app_app)
    registry.add_group("client", client_app)


if __name__ == "__main__":
    start()
