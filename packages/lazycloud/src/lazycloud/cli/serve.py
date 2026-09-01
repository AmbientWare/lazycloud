from __future__ import annotations

from typing import Annotated

import typer
from shared.compute_policy import MachinePool

from lazycloud.cli.components.output import json_output_enabled
from lazycloud.cli.components.progress import attach_terminal
from lazycloud.cli.handler_workflows import (
    HandlerLoadError,
    apply_handler_reference,
    invoke_handler_method,
    load_handler_object,
)
from lazycloud.cli.workflow_options import build_deployment_overrides, workflow_kwargs


def serve(
    ctx: typer.Context,
    handler: Annotated[str, typer.Argument()],
    timeout: Annotated[int, typer.Option("--timeout")] = 0,
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
    container_ports: Annotated[
        list[str] | None,
        typer.Option("--container-port"),
    ] = None,
    keep_warm: Annotated[int | None, typer.Option("--keep-warm", min=0)] = None,
    tcp: Annotated[bool | None, typer.Option("--tcp/--no-tcp")] = None,
    pool: Annotated[str | None, typer.Option("--pool")] = None,
    entrypoint: Annotated[list[str] | None, typer.Option("--entrypoint")] = None,
    sync_dir: Annotated[str | None, typer.Option("--sync-dir", "--sync")] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
) -> None:
    if json_output_enabled(ctx):
        raise typer.BadParameter("--json cannot be used with the live serve stream")
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
        ports=container_ports,
        keep_warm=keep_warm,
        tcp=tcp,
        pool=MachinePool(pool) if pool else None,
        entrypoint=entrypoint,
        sync_dir=sync_dir,
        container_id=container_id,
    )
    try:
        user_object = apply_handler_reference(load_handler_object(handler), handler)
    except HandlerLoadError as exc:
        raise typer.BadParameter(str(exc)) from exc
    attach_terminal(user_object)
    invoke_handler_method(
        user_object,
        "serve",
        kwargs=workflow_kwargs(overrides, timeout=timeout),
    )
