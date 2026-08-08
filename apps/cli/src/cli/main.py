from __future__ import annotations

import sys

import typer
from foundation.environment_file import load_environment_file
from lazycloud.cli.components.errors import (
    debug_errors_enabled,
    json_errors_enabled,
    render_exception,
)
from lazycloud.cli.main import (
    PublicCliRegistry,
    build_public_cli,
    normalize_global_flags,
)

from cli.agent import agent_app
from cli.components.errors import ADMIN_ERROR_POLICY
from cli.control_plane import concurrency_app, stub_app, workspace_app
from cli.database import database_app
from cli.execution import events, invoke
from cli.identity import profile_export, token_create, token_list, token_revoke
from cli.offline_auth import auth_app
from cli.operations import cron_app, image_app, scheduler_app
from cli.resources import (
    container_app,
    map_app,
    queue_app,
    register_machine_extensions,
    unit_app,
    worker_app,
)
from cli.storage import cache_app, object_app
from cli.usage import usage_app

_ADMIN_ROOT_ORDER = (
    "deploy",
    "invoke",
    "run",
    "events",
    "shell",
    "dev",
    "serve",
    "login",
    "logs",
    "quickstart",
    "create-app",
    "ls",
    "cp",
    "rm",
    "mv",
)
_ADMIN_GROUP_ORDER = (
    "profile",
    "token",
    "task",
    "deployment",
    "queue",
    "map",
    "secret",
    "domain",
    "volume",
    "container",
    "unit",
    "machine",
    "worker",
    "image",
    "cron",
    "scheduler",
    "agent",
    "object",
    "cache",
    "workspace",
    "cloud",
    "compute",
    "stub",
    "app",
    "concurrency",
    "database",
    "auth",
    "client",
    "usage",
    "example",
)


def build_admin_cli() -> typer.Typer:
    return build_public_cli((_register_operator_cli,))


def start(args: list[str] | None = None, prog_name: str | None = None) -> None:
    load_environment_file()
    effective_args = normalize_global_flags(list(sys.argv[1:] if args is None else args))
    try:
        build_admin_cli()(args=effective_args, prog_name=prog_name)
    except SystemExit:
        raise
    except Exception as exc:
        if debug_errors_enabled(effective_args):
            raise
        exit_code = render_exception(
            exc,
            json_output=json_errors_enabled(effective_args),
            policy=ADMIN_ERROR_POLICY,
        )
        raise SystemExit(exit_code) from None


def _register_operator_cli(registry: PublicCliRegistry) -> None:
    registry.add_root_command("invoke", _register_invoke)
    registry.add_root_command("events", _register_events)
    registry.extend_group("profile", _register_profile_extensions)
    registry.extend_group("token", _register_token_extensions)
    registry.replace_group("workspace", workspace_app)
    registry.replace_group("container", container_app)
    registry.extend_group("machine", register_machine_extensions)

    registry.add_group("unit", unit_app)
    registry.add_group("queue", queue_app)
    registry.add_group("map", map_app)
    registry.add_group("worker", worker_app)
    registry.add_group("image", image_app)
    registry.add_group("cron", cron_app)
    registry.add_group("scheduler", scheduler_app)
    registry.add_group("agent", agent_app)
    registry.add_group("object", object_app)
    registry.add_group("cache", cache_app)
    registry.add_group("stub", stub_app)
    registry.add_group("concurrency", concurrency_app)
    registry.add_group("database", database_app)
    registry.add_group("auth", auth_app)
    registry.add_group("usage", usage_app)

    registry.order_root_commands(_ADMIN_ROOT_ORDER)
    registry.order_groups(_ADMIN_GROUP_ORDER)


def _register_invoke(application: typer.Typer) -> None:
    application.command("invoke", help="Invoke a deployed resource.")(invoke)


def _register_events(application: typer.Typer) -> None:
    application.command("events", help="Inspect task and container events.")(events)


def _register_profile_extensions(group: typer.Typer) -> None:
    group.command("export")(profile_export)


def _register_token_extensions(group: typer.Typer) -> None:
    group.command("create")(token_create)
    group.command("list")(token_list)
    group.command("revoke")(token_revoke)


if __name__ == "__main__":
    start()
