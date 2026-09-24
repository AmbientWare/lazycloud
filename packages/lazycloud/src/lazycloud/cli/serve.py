from __future__ import annotations

from typing import Annotated

import typer

from lazycloud.abstractions.app import App
from lazycloud.abstractions.endpoint import ASGI, Endpoint
from lazycloud.abstractions.function import Function
from lazycloud.cli.components.output import json_output_enabled
from lazycloud.cli.components.progress import attach_terminal
from lazycloud.cli.handler_workflows import apply_handler_reference, load_handler_object


def serve(
    ctx: typer.Context,
    handler: Annotated[str, typer.Argument()],
    timeout: Annotated[int, typer.Option("--timeout")] = 0,
    sync_dir: Annotated[str | None, typer.Option("--sync-dir", "--sync")] = None,
) -> None:
    if json_output_enabled(ctx):
        raise typer.BadParameter("--json cannot be used with the live serve stream")
    user_object = apply_handler_reference(load_handler_object(handler), handler)
    attach_terminal(user_object)
    if not isinstance(user_object, (App, Function, Endpoint, ASGI)):
        raise typer.BadParameter("serve requires an App, Function, Endpoint, or ASGI handler")
    user_object.serve(timeout=timeout, sync_dir=sync_dir)
