from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Protocol, runtime_checkable

import typer
from pydantic import JsonValue
from shared.api import Deployment

from lazycloud._invocation import prepare_arguments
from lazycloud._terminal.cards import result_card
from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    emit,
    json_output_enabled,
    parse_json_argument,
    print_payload,
)
from lazycloud.cli.components.progress import ConnectingIndicator, attach_terminal
from lazycloud.cli.components.results import emit_python_result
from lazycloud.cli.handler_workflows import (
    apply_handler_reference,
    call_handler,
    invoke_handler_method,
    load_deployment_object,
    load_handler_object,
)
from lazycloud.control import (
    api_client,
    control_workspace_scope,
    require_workspace,
    resolve_control_client_config,
)
from lazycloud.session.deployment import deploy_functions
from lazycloud.terminal_shell import InteractiveShell


@runtime_checkable
class RunWorkflow(Protocol):
    def run(self, *args: Any) -> Any: ...


@runtime_checkable
class RemoteWorkflow(Protocol):
    def remote(self, *args: Any) -> Any: ...


def deploy(
    ctx: typer.Context,
    handler: Annotated[
        list[str], typer.Argument(help="Python files, modules, or module:object references.")
    ],
    prune: Annotated[
        bool,
        typer.Option(
            "--prune", "-p", help="Stop deployed functions omitted from the complete app."
        ),
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    source_root: Annotated[str | None, typer.Option("--source-root")] = None,
) -> None:
    with control_workspace_scope(workspace or ""):
        loaded = [load_deployment_object(reference) for reference in handler]
        apps = [item for item in loaded if isinstance(item, App)]
        if len(loaded) > 1 and len(apps) != len(loaded):
            raise typer.BadParameter("multiple references must select complete apps")
        if prune and len(apps) != len(loaded):
            raise typer.BadParameter("--prune requires complete apps")
        config = resolve_control_client_config(workspace=workspace, timeout_seconds=60)
        selected_workspace = require_workspace(config)
        if apps:
            combined = App.combine(apps)
            terminal = attach_terminal(combined[0])
            for app in combined[1:]:
                attach_terminal(app, terminal)
            deployments = deploy_functions(
                [app.deployment_target(prune=prune) for app in combined],
                client=api_client(config),
                workspace=selected_workspace,
                source_root=source_root,
                terminal=terminal,
            )
        else:
            target = loaded[0]
            attach_terminal(target)
            if isinstance(target, Function | Endpoint | ASGI):
                deployments = [target.deploy(workspace=selected_workspace, source_root=source_root)]
            else:
                invoke_handler_method(
                    target,
                    "deploy",
                    kwargs={"workspace": selected_workspace, "source_root": source_root},
                )
                return
    summaries: list[JsonValue] = [_deployment_summary(item) for item in deployments]
    emit(
        ctx,
        payload=[item.model_dump(mode="json") for item in deployments]
        if len(deployments) > 1
        else deployments[0].model_dump(mode="json"),
        view=result_card(
            summaries[0] if len(summaries) == 1 else {"apps": list(summaries)},
            title="App deployed" if len(summaries) == 1 else "Apps deployed",
            tone="success",
        ),
    )


def _deployment_summary(deployment: Deployment) -> dict[str, JsonValue]:
    summary: dict[str, JsonValue] = {
        "app": deployment.app.name,
        "functions": [f"{release.function} v{release.version}" for release in deployment.releases],
    }
    urls = [release.url for release in deployment.releases if release.url]
    if len(urls) == 1 and len(deployment.releases) == 1:
        summary["url"] = urls[0]
    elif urls:
        summary["urls"] = list(urls)
    if deployment.pruned:
        summary["stopped"] = [item.root for item in deployment.pruned]
    return summary


def run(
    ctx: typer.Context,
    command: Annotated[list[str] | None, typer.Argument()] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Save the result to a .png, .html, .txt, .json, or .pkl file.",
            dir_okay=False,
            writable=True,
        ),
    ] = None,
) -> None:
    """Run a function remotely with JSON arguments; --json sends and returns JSON."""
    args = command or []
    if not args:
        raise typer.BadParameter("handler is required")
    with control_workspace_scope(workspace or ""):
        user_object = _load_run_target(args[0])
        if user_object is None:
            msg = "public client run requires an SDK handler reference"
            raise typer.BadParameter(msg)
        payload_args = [parse_json_argument(item) for item in args[1:]]
        target = apply_handler_reference(user_object, args[0])
        attach_terminal(target)
        if isinstance(target, Function):
            if json_output_enabled(ctx):
                response = target.run_task(target.submit_json(payload_args, {}))
            else:
                prepared_args, prepared_kwargs = prepare_arguments(
                    target.func, tuple(payload_args), {}, target.inputs
                )
                response = call_handler(
                    target.remote, args=list(prepared_args), kwargs=prepared_kwargs
                )
        elif isinstance(target, RunWorkflow):
            response = call_handler(target.run, args=payload_args)
        elif isinstance(target, RemoteWorkflow):
            response = call_handler(target.remote, args=payload_args)
        else:
            response = call_handler(target, args=payload_args)
    emit_python_result(ctx, response, output=output)


def shell(
    ctx: typer.Context,
    handler: Annotated[str | None, typer.Argument()] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
    sync_dir: Annotated[str | None, typer.Option("--sync-dir", "--sync")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    _require_interactive_output(ctx)
    if handler is None:
        if container_id is None:
            raise typer.BadParameter("handler or --container-id is required")
        open_existing_shell(
            ctx,
            container_id=container_id,
            sync_dir=sync_dir,
            workspace=workspace,
        )
        return
    user_object = load_handler_object(handler)
    attach_terminal(user_object)
    target = apply_handler_reference(user_object, handler)
    indicator = ConnectingIndicator(str(getattr(target, "name", handler))).start()
    try:
        response = invoke_handler_method(
            target,
            "shell",
            kwargs={"workspace": workspace, "sync_dir": sync_dir},
        )
        if isinstance(response, ShellSession):
            open_shell_session(ctx, response, workspace=workspace, indicator=indicator)
            return
    finally:
        indicator.connected()
    print_payload(ctx, response)


def open_existing_shell(
    ctx: typer.Context,
    *,
    container_id: str,
    sync_dir: str | None = None,
    workspace: str | None = None,
) -> None:
    _require_interactive_output(ctx)
    indicator = ConnectingIndicator(container_id[:12]).start()
    try:
        shell_client = Shell(
            workspace=current_workspace(workspace),
            interactive_shell=InteractiveShell(on_attached=indicator.connected),
        )
        session = shell_client.create_existing(container_id, sync_dir=sync_dir)
        status = shell_client.connect(session)
    finally:
        indicator.connected()
    _exit_with_shell_status(status)


def open_shell_session(
    ctx: typer.Context,
    session: ShellSession,
    *,
    workspace: str | None = None,
    indicator: ConnectingIndicator | None = None,
) -> None:
    _require_interactive_output(ctx)
    waiting = indicator or ConnectingIndicator(session.container_id[:12]).start()
    try:
        shell_client = Shell(
            workspace=current_workspace(workspace),
            interactive_shell=InteractiveShell(on_attached=waiting.connected),
        )
        status = shell_client.connect(session)
    finally:
        waiting.connected()
    _exit_with_shell_status(status)


def _require_interactive_output(ctx: typer.Context) -> None:
    if json_output_enabled(ctx):
        raise typer.BadParameter("--json cannot be used with an interactive shell")


def _exit_with_shell_status(exit_code: int) -> None:
    if exit_code:
        raise typer.Exit(exit_code)


def _load_run_target(reference: str) -> object | None:
    if ":" not in reference:
        return None
    try:
        return load_handler_object(reference)
    except (ImportError, AttributeError, ValueError) as exc:
        msg = f"could not load handler {reference!r}: {exc}"
        raise typer.BadParameter(msg) from exc
