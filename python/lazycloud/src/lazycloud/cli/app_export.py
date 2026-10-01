from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from lazycloud._terminal.cards import result_card
from lazycloud.cli.components.output import emit
from lazycloud.client_codegen import (
    CLIENT_PACKAGE_ROOT,
    ClientGenerationError,
    write_client_package,
)


def app_export(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App slug to export.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[Path, typer.Option("--output", "-o")] = CLIENT_PACKAGE_ROOT,
    openapi: Annotated[
        list[str] | None,
        typer.Option("--openapi", help="ASGI resource=OpenAPI JSON file. Repeat per resource."),
    ] = None,
    openapi_path: Annotated[
        list[str] | None,
        typer.Option("--openapi-path", help="ASGI resource=/schema/path for schema discovery."),
    ] = None,
) -> None:
    """Generate a typed Python package for an app."""
    schemas: dict[str, Path] = {}
    for entry in openapi or ():
        name, separator, path = entry.partition("=")
        if not separator or not name or not path or name in schemas:
            raise typer.BadParameter("--openapi requires one resource=path per ASGI resource")
        schemas[name] = Path(path)
    paths: dict[str, str] = {}
    for entry in openapi_path or ():
        name, separator, path = entry.partition("=")
        if not separator or not name or not path or name in paths:
            raise typer.BadParameter("--openapi-path requires one resource=/path per ASGI resource")
        paths[name] = path
    try:
        payload = write_client_package(
            app=app,
            workspace=workspace,
            output=output,
            openapi_files=schemas,
            openapi_paths=paths,
        )
    except (ClientGenerationError, ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    emit(
        ctx,
        payload=payload,
        view=result_card(
            {
                "package": payload["package"],
                "path": payload["path"],
                "resources": ", ".join(
                    f"{item['kind']}:{item['name']}" for item in payload["resources"]
                ),
                "ASGI without schema": ", ".join(payload["asgi_without_schema"]) or "none",
            },
            title="Typed package generated",
            tone="success",
        ),
    )


__all__ = ["app_export"]
