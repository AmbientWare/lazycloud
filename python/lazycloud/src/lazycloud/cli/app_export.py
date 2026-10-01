from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def app_export(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App slug to export.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    openapi: Annotated[
        list[str] | None,
        typer.Option("--openapi", help="ASGI resource=OpenAPI JSON file. Repeat per resource."),
    ] = None,
    openapi_path: Annotated[
        list[str] | None,
        typer.Option("--openapi-path", help="ASGI resource=/schema/path for schema discovery."),
    ] = None,
) -> None:
    raise typer.BadParameter("not available")


__all__ = ["app_export"]
