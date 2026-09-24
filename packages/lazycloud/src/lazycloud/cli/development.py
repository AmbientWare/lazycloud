from __future__ import annotations

from typing import Annotated

import typer

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.pod import Pod
from lazycloud.abstractions.shell import ShellSession
from lazycloud.cli.components.output import json_output_enabled, print_payload
from lazycloud.cli.components.progress import attach_terminal
from lazycloud.cli.execution import open_shell_session
from lazycloud.cli.handler_workflows import (
    apply_handler_reference,
    invoke_handler_method,
    load_handler_object,
)


def dev(
    ctx: typer.Context,
    handler: Annotated[str | None, typer.Argument()] = None,
    sync_dir: Annotated[str, typer.Option("--sync", help="Directory to sync.")] = "./",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    if json_output_enabled(ctx):
        raise typer.BadParameter("--json cannot be used with an interactive development session")
    if handler is not None:
        user_object = apply_handler_reference(load_handler_object(handler), handler)
        attach_terminal(user_object)
        response = invoke_handler_method(
            user_object,
            "shell",
            kwargs={"workspace": workspace, "sync_dir": sync_dir},
        )
        if isinstance(response, ShellSession):
            open_shell_session(ctx, response, workspace=workspace)
            return
        print_payload(ctx, response)
        return
    pod = _default_dev_pod()
    attach_terminal(pod)
    pod.workspace = workspace
    response = pod.shell(workspace=workspace, sync_dir=sync_dir)
    if isinstance(response, ShellSession):
        open_shell_session(ctx, response, workspace=workspace)
        return
    print_payload(ctx, response)


def _default_dev_pod() -> Pod:
    return Pod(_app_slug="dev", name="dev", image=Image())


__all__ = ["dev"]
