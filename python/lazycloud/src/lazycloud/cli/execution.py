from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Protocol, cast, runtime_checkable

import typer
from pydantic import JsonValue
from shared.api import (
    Deployment,
    DeploymentPlan,
    DeploymentPlanRequest,
    PodKind,
    Release,
    WorkloadIdentity,
    WorkloadKind,
)

from lazycloud._invocation import prepare_arguments
from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import short_id
from lazycloud._terminal.streams import console
from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.cli.apps import app_name
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    emit,
    json_output_enabled,
    parse_json_argument,
    print_payload,
    table,
)
from lazycloud.cli.components.progress import ConnectingIndicator, attach_terminal
from lazycloud.cli.components.results import emit_python_result
from lazycloud.cli.control import api_session
from lazycloud.cli.handler_workflows import (
    apply_handler_reference,
    call_handler,
    invoke_handler_method,
    load_deployment_object,
    load_handler_object,
)
from lazycloud.clients.api import ApiClient
from lazycloud.control import (
    api_client,
    control_workspace_scope,
    require_workspace,
    resolve_control_client_config,
)
from lazycloud.session.deployment import (
    AppFunctions,
    DeploymentClient,
    WorkloadDefinition,
    deploy_functions,
)
from lazycloud.terminal_shell import InteractiveShell

deployment_app = typer.Typer(help="Manage deployments.")


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
            "--prune", "-p", help="Remove workloads omitted from the complete app definition."
        ),
    ] = False,
    diff: Annotated[
        bool, typer.Option("--diff", "-d", help="Preview deployment actions without deploying.")
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
        client = api_client(config)
        selected_workspace = require_workspace(config)
        if apps:
            combined = App.combine(apps)
            targets = [app.deployment_target(prune=prune) for app in combined]
            if diff:
                _emit_deployment_plans(
                    ctx, [_plan(client, selected_workspace, target) for target in targets]
                )
                return
            terminal = attach_terminal(combined[0])
            for app in combined[1:]:
                attach_terminal(app, terminal)
            deployments = deploy_functions(
                targets,
                client=client,
                workspace=selected_workspace,
                source_root=source_root,
                terminal=terminal,
            )
            _emit_app_deployments(ctx, targets, deployments)
            return
        target = loaded[0]
        attach_terminal(target)
        if diff:
            if not isinstance(target, Function | Endpoint | ASGI | Pod):
                raise typer.BadParameter("--diff requires an app or a decorated workload")
            workload = cast("WorkloadDefinition", target)
            plan = _plan(client, selected_workspace, AppFunctions(workload._app_slug, (workload,)))
            _emit_deployment_plans(ctx, [plan])
            return
        if not isinstance(target, Function | Endpoint | ASGI | Pod):
            invoke_handler_method(
                target,
                "deploy",
                kwargs={"workspace": selected_workspace, "source_root": source_root},
            )
            return
        deployment = target.deploy(workspace=selected_workspace, source_root=source_root)
    release = next(item for item in deployment.releases if item.name == target.resource_name)
    emit(
        ctx,
        payload=deployment.model_dump(mode="json"),
        view=result_card(
            _release_summary(handler[0], release), title="Deployment created", tone="success"
        ),
    )


def _release_summary(name: str, release: Release) -> dict[str, JsonValue]:
    summary: dict[str, JsonValue] = {"name": name}
    if release.version is not None:
        summary["version"] = release.version
    if release.url:
        summary["url"] = release.url
    spec = release.spec
    if spec.pod is not None and spec.pod.kind is not PodKind.sandbox:
        summary["role"] = spec.pod.kind.value
        if spec.keep_warm_seconds is not None:
            summary["keep_warm"] = (
                "always" if spec.keep_warm_seconds == -1 else f"{spec.keep_warm_seconds}s"
            )
        summary["preemptible"] = spec.placement.preemptible if spec.placement else True
    return summary


def _plan(client: ApiClient, workspace: str, target: AppFunctions) -> DeploymentPlan:
    request = DeploymentPlanRequest(
        workloads=[
            WorkloadIdentity(kind=_workload_kind(function), name=function.resource_name)
            for function in target.functions
        ],
        prune=target.prune,
    )
    return client.plan_deployment(workspace, target.app, request)


def _workload_kind(workload: object) -> WorkloadKind:
    if isinstance(workload, Pod):
        return WorkloadKind.pod
    if isinstance(workload, Endpoint):
        return WorkloadKind.endpoint
    if isinstance(workload, ASGI):
        return WorkloadKind.asgi
    return WorkloadKind.function


def _emit_deployment_plans(ctx: typer.Context, plans: list[DeploymentPlan]) -> None:
    payload: JsonValue = (
        plans[0].model_dump(mode="json")
        if len(plans) == 1
        else {"apps": [plan.model_dump(mode="json") for plan in plans]}
    )
    emit(
        ctx,
        payload=payload,
        view=table(
            "deployment actions",
            ["App", "Kind", "Workload", "Action", "Existing versions"],
            [
                [plan.app, item.kind, item.name, item.action.value, item.versions]
                for plan in plans
                for item in plan.items
            ],
        ),
    )


def _emit_app_deployments(
    ctx: typer.Context, targets: Sequence[AppFunctions], deployments: Sequence[Deployment]
) -> None:
    summaries: list[JsonValue] = []
    for target, deployment in zip(targets, deployments, strict=True):
        summary: dict[str, JsonValue] = {
            "app": deployment.app.name,
            "workloads": len(deployment.releases),
        }
        urls: list[JsonValue] = [release.url for release in deployment.releases if release.url]
        if urls:
            summary["urls"] = urls
        devboxes: list[JsonValue] = [
            release.name
            for release in deployment.releases
            if release.spec.pod is not None and release.spec.pod.kind is PodKind.devbox
        ]
        if devboxes:
            summary["devboxes"] = devboxes
        if target.prune:
            summary["removed_versions"] = deployment.removed_versions
        summaries.append(summary)
    payload: JsonValue = (
        deployments[0].model_dump(mode="json")
        if len(deployments) == 1
        else {"apps": [item.model_dump(mode="json") for item in deployments]}
    )
    emit(
        ctx,
        payload=payload,
        view=result_card(
            summaries[0] if len(summaries) == 1 else summaries,
            title="App deployed" if len(summaries) == 1 else "Apps deployed",
            tone="success",
        ),
    )


@deployment_app.command("list", help="List deployments, optionally filtered by app.")
def deployment_list(
    ctx: typer.Context,
    app: Annotated[str | None, typer.Option("--app")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    selected_app = app_name(client, selected_workspace, app) if app else None
    deployments = DeploymentClient(workspace=selected_workspace, client=client).list(
        app=selected_app, limit=limit
    )
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in deployments])
        return
    rows: list[list[Any]] = [
        [item.name, item.kind, item.version, item.state.value] for item in deployments
    ]
    console.print(table("Deployments", ["name", "kind", "version", "status"], rows))


@deployment_app.command("stop", help="Stop one or more deployments.")
def deployment_stop(
    ctx: typer.Context,
    deployment_ids_or_names: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    deployments = DeploymentClient(workspace=workspace)
    responses: list[JsonValue] = [
        deployments.stop(reference).model_dump(mode="json") for reference in deployment_ids_or_names
    ]
    emit(
        ctx,
        payload=responses,
        view=notice_card(
            f"Stopped {len(responses)} deployment{'s' if len(responses) != 1 else ''}.",
            tone="success",
        ),
    )


@deployment_app.command("start", help="Start a stopped deployment.")
def deployment_start(
    ctx: typer.Context,
    deployment_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = DeploymentClient(workspace=workspace).start(deployment_id_or_name)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(f"Started {response.name}.", tone="success"),
    )


@deployment_app.command("scale", help="Set a deployment's container count.")
def deployment_scale(
    ctx: typer.Context,
    deployment_id_or_name: str,
    containers: Annotated[int, typer.Option("--containers", min=0)],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = DeploymentClient(workspace=workspace).scale(deployment_id_or_name, containers)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(f"Set {response.name} to {containers} containers.", tone="success"),
    )


@deployment_app.command("delete", help="Delete a deployment.")
def deployment_delete(
    ctx: typer.Context,
    deployment_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    DeploymentClient(workspace=workspace).delete(deployment_id_or_name)
    emit(
        ctx,
        payload={"deployment_id": deployment_id_or_name, "deleted": True},
        view=notice_card(f"Deleted {deployment_id_or_name}.", tone="success"),
    )


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
        if isinstance(target, Pod):
            response = target.run(*args[1:], workspace=workspace)
        elif isinstance(target, Function):
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
    indicator = ConnectingIndicator(short_id(container_id, 12)).start()
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
    waiting = indicator or ConnectingIndicator(short_id(session.container_id, 12)).start()
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
