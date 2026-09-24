from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Annotated, Any, Protocol, runtime_checkable

import typer
from pydantic import JsonValue
from shared.app_slug import app_slug_or_default
from shared.deployments import PodRole
from shared.http.deployment_plans import (
    DeploymentPlanRequest,
    DeploymentPlanResponse,
    WorkloadIdentity,
)
from shared.http.gateway import DeployStubResponse

from lazycloud._invocation import prepare_arguments
from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.streams import console
from lazycloud.abstractions.app import App, AppDeployResult
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.cli.apps import resolve_app_id
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    emit,
    json_default,
    json_output_enabled,
    parse_json_argument,
    print_payload,
    table,
)
from lazycloud.cli.components.progress import ConnectingIndicator, attach_terminal
from lazycloud.cli.components.results import emit_python_result
from lazycloud.cli.control import resource_client
from lazycloud.cli.handler_workflows import (
    HandlerLoadError,
    apply_handler_reference,
    call_handler,
    invoke_handler_method,
    load_deployment_object,
    load_handler_object,
)
from lazycloud.control import control_workspace_scope, resolve_control_client_config
from lazycloud.json_contracts import resource_payload
from lazycloud.session.app_deployment import AppDeploymentSession, AppDeploymentTarget
from lazycloud.session.deployment import DeploymentClient
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
    selected_workspace = workspace or resolve_control_client_config().workspace
    with control_workspace_scope(selected_workspace):
        try:
            loaded = [load_deployment_object(reference) for reference in handler]
        except HandlerLoadError as exc:
            raise typer.BadParameter(str(exc)) from exc
        apps = [item for item in loaded if isinstance(item, App)]
        if len(loaded) > 1 and len(apps) != len(loaded):
            raise typer.BadParameter("multiple references must select complete apps")
        if prune and len(apps) != len(loaded):
            raise typer.BadParameter("--prune requires complete apps")
        if apps:
            _deploy_apps(
                ctx,
                App.combine(apps),
                workspace=selected_workspace,
                source_root=source_root,
                prune=prune,
                diff=diff,
            )
            return
        user_object = loaded[0]
        selected_handler = handler[0]
        attach_terminal(user_object)
        if diff:
            if not isinstance(user_object, (Pod, Function, Endpoint, ASGI)):
                raise typer.BadParameter("--diff requires an app or a decorated workload")
            spec = user_object.spec()
            app_name = spec.metadata.get("app")
            plan = resource_client(workspace=selected_workspace).plan_deployment(
                DeploymentPlanRequest(
                    app=app_slug_or_default(
                        app_name if isinstance(app_name, str) else None,
                        default=spec.name,
                    ),
                    workloads=[WorkloadIdentity(kind=spec.kind, name=spec.name)],
                )
            )
            _emit_deployment_plans(ctx, [plan])
            return
        if isinstance(user_object, (Pod, Function)):
            response = user_object.deploy(
                workspace=selected_workspace,
                source_root=source_root,
            )
        else:
            response = invoke_handler_method(
                user_object,
                "deploy",
                kwargs={
                    "workspace": selected_workspace,
                    "source_root": source_root,
                },
            )
    payload = resource_payload(response)
    if isinstance(user_object, (App, Function, Pod)):
        emit(
            ctx,
            payload=payload,
            view=result_card(
                json_default(_deployment_summary(response, handler=selected_handler)),
                title="App deployed"
                if isinstance(response, AppDeployResult)
                else "Deployment created",
                tone="success",
            ),
        )
        return
    print_payload(ctx, payload)


def _deploy_apps(
    ctx: typer.Context,
    apps: tuple[App, ...],
    *,
    workspace: str,
    source_root: str | None,
    prune: bool,
    diff: bool,
) -> None:

    def submit(app: App) -> tuple[DeployStubResponse, ...]:
        if prune and not app.deployment_manifest().workloads:
            return ()
        attach_terminal(app)
        return app.deploy(
            workspace=workspace,
            source_root=source_root,
        ).resources

    targets: list[AppDeploymentTarget] = []
    for app in apps:
        manifest = app.deployment_manifest(prune=prune)
        targets.append(AppDeploymentTarget(manifest, partial(submit, app)))
    session = AppDeploymentSession(
        resource_client(workspace=workspace, timeout_seconds=60),
        terminal=attach_terminal(apps[0]),
    )
    if diff:
        _emit_deployment_plans(ctx, session.preview(targets))
        return
    outcomes = session.deploy(targets)
    results = [AppDeployResult(item.app, item.resources, item.pruning) for item in outcomes]
    payload: JsonValue = (
        results[0].model_dump()
        if len(results) == 1
        else {
            "apps": [item.model_dump() for item in results],
        }
    )
    summaries = [_deployment_summary(result, handler=result.app) for result in results]
    emit(
        ctx,
        payload=payload,
        view=result_card(
            json_default(summaries[0] if len(summaries) == 1 else summaries),
            title="App deployed" if len(summaries) == 1 else "Apps deployed",
            tone="success",
        ),
    )


def _emit_deployment_plans(ctx: typer.Context, plans: list[DeploymentPlanResponse]) -> None:
    payload = (
        plans[0].model_dump(mode="json")
        if len(plans) == 1
        else {
            "apps": [plan.model_dump(mode="json") for plan in plans],
        }
    )
    emit(
        ctx,
        payload=payload,
        view=table(
            "deployment actions",
            ["App", "Kind", "Workload", "Action", "Existing versions"],
            [
                [plan.app, item.kind.value, item.name, item.action, item.versions]
                for plan in plans
                for item in plan.data
            ],
        ),
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
    args = command or []
    if not args:
        raise typer.BadParameter("handler is required")
    selected_workspace = workspace or resolve_control_client_config().workspace
    with control_workspace_scope(selected_workspace):
        user_object = _load_run_target(args[0])
        if user_object is None:
            msg = "public client run requires an SDK handler reference"
            raise typer.BadParameter(msg)
        payload_args = [parse_json_argument(item) for item in args[1:]]
        target = apply_handler_reference(user_object, args[0])
        attach_terminal(target)
        if isinstance(target, Pod):
            response = target.run(*args[1:], workspace=selected_workspace)
        elif isinstance(target, Function):
            prepared_args, prepared_kwargs = prepare_arguments(
                target.func, tuple(payload_args), {}, target.inputs
            )
            response = call_handler(target.remote, args=list(prepared_args), kwargs=prepared_kwargs)
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
    try:
        user_object = load_handler_object(handler)
    except HandlerLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc
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


@deployment_app.command("list", help="List deployments, optionally filtered by app.")
def deployment_list(
    ctx: typer.Context,
    app: Annotated[str | None, typer.Option("--app")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    selected_workspace = current_workspace(workspace)
    app_id = (
        resolve_app_id(app, client=resource_client(workspace=selected_workspace)) if app else None
    )
    deployments = DeploymentClient(workspace=selected_workspace).list(
        filters={"app_id": [app_id]} if app_id else None,
        limit=limit,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in deployments])
        return
    rows: list[list[Any]] = [
        [item.name, item.kind.value, item.version, "active" if item.active else "stopped"]
        for item in deployments
    ]
    console.print(table("Deployments", ["name", "kind", "version", "status"], rows))


@deployment_app.command("stop", help="Stop one or more deployments.")
def deployment_stop(
    ctx: typer.Context,
    deployment_ids_or_names: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    responses: list[Any] = []
    client = DeploymentClient(workspace=current_workspace(workspace))
    selected_workspace = current_workspace(workspace)
    for deployment_id in deployment_ids_or_names:
        response = client.stop(deployment_id, workspace=selected_workspace)
        responses.append(response.model_dump(mode="json"))
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
    response = DeploymentClient(workspace=current_workspace(workspace)).start(deployment_id_or_name)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            f"Started {response.name}.",
            tone="success",
        ),
    )


@deployment_app.command("scale", help="Set a deployment's container count.")
def deployment_scale(
    ctx: typer.Context,
    deployment_id_or_name: str,
    containers: Annotated[int, typer.Option("--containers", min=0)],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = DeploymentClient(workspace=current_workspace(workspace)).scale(
        deployment_id_or_name,
        containers,
    )
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            f"Set {response.name} to {containers} containers.",
            tone="success",
        ),
    )


@deployment_app.command("delete", help="Delete a deployment.")
def deployment_delete(
    ctx: typer.Context,
    deployment_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    DeploymentClient(workspace=current_workspace(workspace)).delete(deployment_id_or_name)
    emit(
        ctx,
        payload={"deployment_id": deployment_id_or_name, "deleted": True},
        view=notice_card(
            f"Deleted {deployment_id_or_name}.",
            tone="success",
        ),
    )


def _deployment_summary(
    response: object,
    *,
    handler: str,
) -> dict[str, object]:
    if isinstance(response, AppDeployResult):
        summary: dict[str, object] = {
            "app": response.app,
            "workloads": len(response.resources),
        }
        urls = [
            invoke_url
            for resource in response.resources
            if (invoke_url := getattr(resource, "invoke_url", ""))
        ]
        if urls:
            summary["urls"] = urls
        devboxes = [
            resource.name for resource in response.resources if resource.role is PodRole.Devbox
        ]
        if devboxes:
            summary["devboxes"] = devboxes
        if response.pruning is not None:
            summary["removed_versions"] = response.pruning.removed_versions
        return summary

    summary = {"name": handler}
    version = getattr(response, "version", 0)
    if version:
        summary["version"] = version
    invoke_url = getattr(response, "invoke_url", "")
    if invoke_url:
        summary["url"] = invoke_url
    if isinstance(response, DeployStubResponse) and response.role is not None:
        summary["role"] = response.role.value
        if response.keep_warm_seconds is not None:
            summary["keep_warm"] = (
                "always" if response.keep_warm_seconds == -1 else f"{response.keep_warm_seconds}s"
            )
        if response.preemptible is not None:
            summary["preemptible"] = response.preemptible
    return summary


def _load_run_target(reference: str) -> object | None:
    try:
        return load_handler_object(reference)
    except (
        HandlerLoadError,
        ImportError,
        AttributeError,
        ModuleNotFoundError,
        ValueError,
    ) as exc:
        if ":" in reference:
            msg = f"could not load handler {reference!r}: {exc}"
            raise typer.BadParameter(msg) from exc
    return None
