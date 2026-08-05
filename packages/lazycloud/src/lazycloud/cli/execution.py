from __future__ import annotations

from typing import Annotated, Any, Protocol, runtime_checkable
from uuid import UUID

import typer

from lazycloud.abstractions.app import App
from lazycloud.abstractions.function import Function
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.serve import sync_local_workspace
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    console,
    json_output_enabled,
    parse_json_argument,
    payload_data,
    print_payload,
    table,
)
from lazycloud.cli.control import resource_client
from lazycloud.cli.handler_workflows import (
    HandlerLoadError,
    apply_handler_reference,
    call_handler,
    invoke_handler_method,
    load_handler_object,
)
from lazycloud.cli.workflow_options import (
    DeploymentOverrides,
    build_deployment_overrides,
    workflow_kwargs,
)
from lazycloud.control import control_workspace_scope, resolve_control_client_config
from lazycloud.control_clients import gateway_control_client
from lazycloud.session.deployment import DeploymentClient

deployment_app = typer.Typer(help="Manage deployments.")


@runtime_checkable
class RunWorkflow(Protocol):
    def run(self, *args: Any) -> Any: ...


@runtime_checkable
class RemoteWorkflow(Protocol):
    def remote(self, *args: Any) -> Any: ...


def deploy(
    ctx: typer.Context,
    handler: str,
    name: Annotated[str | None, typer.Option("--name")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    source_root: Annotated[str | None, typer.Option("--source-root")] = None,
    resource: Annotated[str | None, typer.Option("--resource")] = None,
    cpu: Annotated[float | None, typer.Option("--cpu")] = None,
    memory: Annotated[str | None, typer.Option("--memory")] = None,
    gpu: Annotated[str | None, typer.Option("--gpu")] = None,
    gpu_count: Annotated[int | None, typer.Option("--gpu-count", min=0)] = None,
    image: Annotated[str | None, typer.Option("--image")] = None,
    dockerfile: Annotated[str | None, typer.Option("--dockerfile")] = None,
    context_dir: Annotated[str | None, typer.Option("--context")] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    secrets: Annotated[list[str] | None, typer.Option("--secret")] = None,
    ports: Annotated[list[str] | None, typer.Option("--port")] = None,
    keep_warm: Annotated[int | None, typer.Option("--keep-warm", min=-1)] = None,
    tcp: Annotated[bool | None, typer.Option("--tcp/--no-tcp")] = None,
    pool: Annotated[str | None, typer.Option("--pool")] = None,
    preemptible: Annotated[
        bool | None,
        typer.Option("--preemptible/--no-preemptible"),
    ] = None,
    entrypoint: Annotated[list[str] | None, typer.Option("--entrypoint")] = None,
) -> None:
    overrides = build_deployment_overrides(
        resource=resource,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        gpu_count=gpu_count,
        image=image,
        dockerfile=dockerfile,
        context_dir=context_dir,
        env=env,
        secrets=secrets,
        ports=ports,
        keep_warm=keep_warm,
        tcp=tcp,
        pool=pool,
        preemptible=preemptible,
        entrypoint=entrypoint,
    )
    selected_workspace = workspace or resolve_control_client_config().workspace
    with control_workspace_scope(selected_workspace):
        try:
            user_object = load_handler_object(handler)
        except HandlerLoadError as exc:
            raise typer.BadParameter(str(exc)) from exc
        user_object: object = apply_handler_reference(user_object, handler)
        deployment_image = _deployment_image(overrides)
        if isinstance(user_object, Pod):
            _configure_pod(user_object, overrides)
        elif isinstance(user_object, Function):
            _validate_function_overrides(overrides)
            user_object.configure(
                image=deployment_image,
                cpu=overrides.cpu,
                memory=overrides.memory,
                gpu=overrides.gpu,
                gpu_count=overrides.gpu_count,
                env=overrides.env,
                secrets=overrides.secrets,
                pool=overrides.pool,
                preemptible=overrides.preemptible,
            )
        elif not isinstance(user_object, App) and overrides.has_values():
            msg = "deployment overrides require an App, Function, or Pod handler"
            raise typer.BadParameter(msg)
        if isinstance(user_object, App):
            response = user_object.deploy(
                resource=overrides.resource,
                name=name,
                workspace=selected_workspace,
                source_root=source_root,
                image=deployment_image,
                cpu=overrides.cpu,
                memory=overrides.memory,
                gpu=overrides.gpu,
                gpu_count=overrides.gpu_count,
                env=overrides.env,
                secrets=overrides.secrets,
                ports=overrides.ports,
                keep_warm=overrides.keep_warm,
                tcp=overrides.tcp,
                pool=overrides.pool,
                preemptible=overrides.preemptible,
                entrypoint=overrides.entrypoint,
            )
        elif isinstance(user_object, (Pod, Function)):
            response = user_object.deploy(
                name=name,
                workspace=selected_workspace,
                source_root=source_root,
            )
        else:
            response = invoke_handler_method(
                user_object,
                "deploy",
                kwargs={
                    "workspace": selected_workspace,
                    "name": name,
                    "source_root": source_root,
                },
            )
    print_payload(ctx, payload_data(response))


def run(
    ctx: typer.Context,
    command: Annotated[list[str] | None, typer.Argument()] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    resource: Annotated[str | None, typer.Option("--resource")] = None,
    cpu: Annotated[float | None, typer.Option("--cpu")] = None,
    memory: Annotated[str | None, typer.Option("--memory")] = None,
    gpu: Annotated[str | None, typer.Option("--gpu")] = None,
    gpu_count: Annotated[int | None, typer.Option("--gpu-count", min=0)] = None,
    image: Annotated[str | None, typer.Option("--image")] = None,
    dockerfile: Annotated[str | None, typer.Option("--dockerfile")] = None,
    context_dir: Annotated[str | None, typer.Option("--context")] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    secrets: Annotated[list[str] | None, typer.Option("--secret")] = None,
    ports: Annotated[list[str] | None, typer.Option("--port")] = None,
    keep_warm: Annotated[int | None, typer.Option("--keep-warm", min=-1)] = None,
    tcp: Annotated[bool | None, typer.Option("--tcp/--no-tcp")] = None,
    pool: Annotated[str | None, typer.Option("--pool")] = None,
    preemptible: Annotated[
        bool | None,
        typer.Option("--preemptible/--no-preemptible"),
    ] = None,
    entrypoint: Annotated[list[str] | None, typer.Option("--entrypoint")] = None,
) -> None:
    args = command or []
    if not args:
        raise typer.BadParameter("handler is required")
    overrides = build_deployment_overrides(
        resource=resource,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        gpu_count=gpu_count,
        image=image,
        dockerfile=dockerfile,
        context_dir=context_dir,
        env=env,
        secrets=secrets,
        ports=ports,
        keep_warm=keep_warm,
        tcp=tcp,
        pool=pool,
        preemptible=preemptible,
        entrypoint=entrypoint,
    )
    selected_workspace = workspace or resolve_control_client_config().workspace
    with control_workspace_scope(selected_workspace):
        user_object = _load_run_target(args[0])
        if user_object is None:
            msg = "public client run requires an SDK handler reference"
            raise typer.BadParameter(msg)
        payload_args = [parse_json_argument(item) for item in args[1:]]
        target = apply_handler_reference(user_object, args[0])
        if isinstance(target, Pod):
            _configure_pod(target, overrides)
            response = target.run(*args[1:], workspace=selected_workspace)
        elif isinstance(target, Function):
            _validate_function_overrides(overrides)
            target.configure(
                image=_deployment_image(overrides),
                cpu=overrides.cpu,
                memory=overrides.memory,
                gpu=overrides.gpu,
                gpu_count=overrides.gpu_count,
                env=overrides.env,
                secrets=overrides.secrets,
                pool=overrides.pool,
                preemptible=overrides.preemptible,
            )
            response = call_handler(target.remote, args=payload_args)
        elif isinstance(target, RunWorkflow):
            _reject_unapplied_overrides(target, overrides)
            response = call_handler(target.run, args=payload_args)
        elif isinstance(target, RemoteWorkflow):
            _reject_unapplied_overrides(target, overrides)
            response = call_handler(target.remote, args=payload_args)
        else:
            _reject_unapplied_overrides(target, overrides)
            response = call_handler(target, args=payload_args)
    print_payload(ctx, payload_data(response))


def shell(
    ctx: typer.Context,
    handler: Annotated[str | None, typer.Argument()] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
    sync_dir: Annotated[str | None, typer.Option("--sync-dir", "--sync")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    resource: Annotated[str | None, typer.Option("--resource")] = None,
    cpu: Annotated[float | None, typer.Option("--cpu")] = None,
    memory: Annotated[str | None, typer.Option("--memory")] = None,
    gpu: Annotated[str | None, typer.Option("--gpu")] = None,
    gpu_count: Annotated[int | None, typer.Option("--gpu-count", min=0)] = None,
    image: Annotated[str | None, typer.Option("--image")] = None,
    dockerfile: Annotated[str | None, typer.Option("--dockerfile")] = None,
    context_dir: Annotated[str | None, typer.Option("--context")] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    secrets: Annotated[list[str] | None, typer.Option("--secret")] = None,
    ports: Annotated[list[str] | None, typer.Option("--port")] = None,
    keep_warm: Annotated[int | None, typer.Option("--keep-warm", min=-1)] = None,
    tcp: Annotated[bool | None, typer.Option("--tcp/--no-tcp")] = None,
    pool: Annotated[str | None, typer.Option("--pool")] = None,
    entrypoint: Annotated[list[str] | None, typer.Option("--entrypoint")] = None,
) -> None:
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
    overrides = build_deployment_overrides(
        resource=resource,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        gpu_count=gpu_count,
        image=image,
        dockerfile=dockerfile,
        context_dir=context_dir,
        env=env,
        secrets=secrets,
        ports=ports,
        keep_warm=keep_warm,
        tcp=tcp,
        pool=pool,
        entrypoint=entrypoint,
        sync_dir=sync_dir,
        container_id=container_id,
    )
    try:
        user_object = load_handler_object(handler)
    except HandlerLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if isinstance(user_object, Pod):
        _configure_pod(user_object, overrides)
    response = invoke_handler_method(
        apply_handler_reference(user_object, handler),
        "shell",
        kwargs=workflow_kwargs(overrides, workspace=workspace),
    )
    if isinstance(response, ShellSession):
        open_shell_session(ctx, response, workspace=workspace)
        return
    print_payload(ctx, response)


def open_existing_shell(
    ctx: typer.Context,
    *,
    container_id: str,
    sync_dir: str | None = None,
    workspace: str | None = None,
) -> None:
    _require_interactive_output(ctx)
    selected_workspace = current_workspace(workspace)
    shell_client = Shell(workspace=selected_workspace)
    session = shell_client.create_existing(container_id)
    if sync_dir:
        config = resolve_control_client_config(workspace=selected_workspace)
        sync_local_workspace(
            container_id=container_id,
            local_dir=sync_dir,
            gateway_client=gateway_control_client(config),
        )
    _exit_with_shell_status(shell_client.connect(session))


def open_shell_session(
    ctx: typer.Context,
    session: ShellSession,
    *,
    workspace: str | None = None,
) -> None:
    _require_interactive_output(ctx)
    shell_client = Shell(workspace=current_workspace(workspace))
    _exit_with_shell_status(shell_client.connect(session))


def _require_interactive_output(ctx: typer.Context) -> None:
    if json_output_enabled(ctx):
        raise typer.BadParameter("--json cannot be used with an interactive shell")


def _exit_with_shell_status(exit_code: int) -> None:
    if exit_code:
        raise typer.Exit(exit_code)


def _resolve_app_id(app: str, *, workspace: str | None) -> str:
    """Accept an app name or id for `--app`.

    Apps are addressed by name everywhere a user can see one, and no command
    prints an app id, so a name has to resolve here rather than reach the API as
    a malformed identifier.
    """
    try:
        UUID(app)
    except ValueError:
        pass
    else:
        return app
    apps = resource_client(workspace=workspace).list_apps()
    matches = [item for item in apps.data if item.name == app]
    if not matches:
        raise typer.BadParameter(f"no app named {app!r} in workspace {workspace or 'default'}")
    return matches[0].id


@deployment_app.command("list")
def deployment_list(
    ctx: typer.Context,
    app: Annotated[str | None, typer.Option("--app")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    selected_workspace = current_workspace(workspace)
    app_id = _resolve_app_id(app, workspace=selected_workspace) if app else None
    deployments = DeploymentClient(workspace=selected_workspace).list(
        filters={"app_id": [app_id]} if app_id else None,
        limit=limit,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in deployments])
        return
    rows: list[list[Any]] = [
        [item.id, item.name, item.kind.value, item.version, item.app_id or "", item.active]
        for item in deployments
    ]
    console.print(table("Deployments", ["id", "name", "kind", "version", "app", "active"], rows))


@deployment_app.command("stop")
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
    print_payload(ctx, responses)


@deployment_app.command("start")
def deployment_start(
    ctx: typer.Context,
    deployment_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = DeploymentClient(workspace=current_workspace(workspace)).start(deployment_id_or_name)
    print_payload(ctx, response.model_dump(mode="json"))


@deployment_app.command("scale")
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
    print_payload(ctx, response.model_dump(mode="json"))


@deployment_app.command("delete")
def deployment_delete(
    ctx: typer.Context,
    deployment_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    DeploymentClient(workspace=current_workspace(workspace)).delete(deployment_id_or_name)
    print_payload(ctx, {"deployment_id": deployment_id_or_name, "deleted": True})


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


def _configure_pod(pod: Pod, overrides: DeploymentOverrides) -> None:
    image = _deployment_image(overrides)
    pod.configure(
        image=image,
        command=overrides.entrypoint,
        ports=overrides.ports,
        env=overrides.env,
        cpu=overrides.cpu,
        memory=overrides.memory,
        gpu=overrides.gpu,
        gpu_count=overrides.gpu_count,
        keep_warm=overrides.keep_warm,
        secrets=overrides.secrets,
        tcp=overrides.tcp,
        pool=overrides.pool,
        preemptible=overrides.preemptible,
    )


def _validate_function_overrides(overrides: DeploymentOverrides) -> None:
    unsupported: list[str] = []
    if overrides.resource:
        unsupported.append("resource")
    if overrides.ports:
        unsupported.append("ports")
    if overrides.keep_warm is not None:
        unsupported.append("keep_warm")
    if overrides.tcp is not None:
        unsupported.append("tcp")
    if overrides.entrypoint:
        unsupported.append("entrypoint")
    if unsupported:
        options = ", ".join(unsupported)
        raise typer.BadParameter(f"Function does not support overrides: {options}")


def _deployment_image(overrides: DeploymentOverrides) -> Image | None:
    if overrides.image and overrides.dockerfile:
        raise typer.BadParameter("use either --image or --dockerfile, not both")
    if overrides.context_dir and not overrides.dockerfile:
        raise typer.BadParameter("--context requires --dockerfile")
    if overrides.dockerfile:
        return Image.from_dockerfile(
            overrides.dockerfile,
            context_dir=overrides.context_dir,
        )
    if overrides.image:
        return Image.from_registry(overrides.image)
    return None


def _reject_unapplied_overrides(target: object, overrides: DeploymentOverrides) -> None:
    if not overrides.has_values():
        return
    target_name = type(target).__name__
    msg = f"{target_name} does not support deployment overrides"
    raise typer.BadParameter(msg)
