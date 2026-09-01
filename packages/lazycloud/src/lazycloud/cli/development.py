from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from shared.compute_policy import MachinePool

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.pod import Pod
from lazycloud.cli.components.output import json_output_enabled, print_payload
from lazycloud.cli.components.progress import attach_terminal
from lazycloud.cli.handler_workflows import (
    HandlerLoadError,
    apply_handler_reference,
    invoke_handler_method,
    load_handler_object,
)
from lazycloud.cli.workflow_options import (
    DeploymentOverrides,
    build_deployment_overrides,
    workflow_kwargs,
)


def dev(
    ctx: typer.Context,
    handler: Annotated[str | None, typer.Argument()] = None,
    sync_dir: Annotated[str, typer.Option("--sync", help="Directory to sync.")] = "./",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    cpu: Annotated[float | None, typer.Option("--cpu")] = None,
    memory: Annotated[str | None, typer.Option("--memory")] = None,
    gpu: Annotated[
        list[str] | None,
        typer.Option(
            "--gpu",
            help=(
                "GPU model to run on. Repeat for a preference, best first: "
                "--gpu h100 --gpu l4 takes an H100 where one is free and an L4 otherwise."
            ),
        ),
    ] = None,
    gpu_count: Annotated[int | None, typer.Option("--gpu-count", min=0)] = None,
    image: Annotated[str | None, typer.Option("--image")] = None,
    dockerfile: Annotated[str | None, typer.Option("--dockerfile")] = None,
    context_dir: Annotated[str | None, typer.Option("--context")] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    secrets: Annotated[list[str] | None, typer.Option("--secret")] = None,
    ports: Annotated[list[str] | None, typer.Option("--port")] = None,
    pool: Annotated[str | None, typer.Option("--pool")] = None,
    entrypoint: Annotated[list[str] | None, typer.Option("--entrypoint")] = None,
) -> None:
    if json_output_enabled(ctx):
        raise typer.BadParameter("--json cannot be used with an interactive development session")
    overrides = build_deployment_overrides(
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
        pool=MachinePool(pool) if pool else None,
        entrypoint=entrypoint,
        sync_dir=sync_dir,
    )
    if handler is not None:
        try:
            user_object = apply_handler_reference(load_handler_object(handler), handler)
        except HandlerLoadError as exc:
            raise typer.BadParameter(str(exc)) from exc
        attach_terminal(user_object)
        response = invoke_handler_method(
            user_object,
            "shell",
            kwargs=workflow_kwargs(overrides, workspace=workspace),
        )
        print_payload(ctx, response)
        return
    pod = _default_dev_pod(overrides)
    attach_terminal(pod)
    pod.workspace = workspace
    response = pod.shell(workspace=workspace, sync_dir=sync_dir)
    print_payload(ctx, response, title="Development session")


def _default_dev_pod(overrides: DeploymentOverrides) -> Pod:
    if overrides.image and overrides.dockerfile:
        raise typer.BadParameter("use either --image or --dockerfile, not both")
    if overrides.dockerfile:
        selected_image = Image.from_dockerfile(
            Path(overrides.dockerfile),
            context_dir=Path(overrides.context_dir) if overrides.context_dir else None,
        )
    elif overrides.image:
        selected_image = Image.from_registry(overrides.image)
    else:
        selected_image = Image()
    return Pod(
        _app_slug="dev",
        name="dev",
        image=selected_image,
        command=list(overrides.entrypoint),
        ports=dict(overrides.ports),
        env=dict(overrides.env),
        cpu=overrides.cpu,
        memory=overrides.memory,
        gpu=list(overrides.gpu or ()),
        gpu_count=overrides.gpu_count or 0,
        secrets=list(overrides.secrets),
        pool=overrides.pool,
    )


__all__ = ["dev"]
