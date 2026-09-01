from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from lazycloud.cli.components.cards import notice_card, result_card
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import emit
from lazycloud.client_codegen import (
    CLIENT_PACKAGE_ROOT,
    remove_client_package,
    write_client_package,
)
from lazycloud.exceptions import ClientGenerationError

client_app = typer.Typer(help="Generate typed clients for deployed apps.")


@client_app.command("get", help="Generate a typed client for an app.")
def get_client(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App slug to fetch.")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[Path, typer.Option("--output", "-o")] = CLIENT_PACKAGE_ROOT,
) -> None:
    try:
        payload = write_client_package(
            app=app,
            workspace=current_workspace(workspace),
            output=output,
        )
    except ClientGenerationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    resources = payload.get("resources")
    resource_count = len(resources) if isinstance(resources, list) else 0
    emit(
        ctx,
        payload=payload,
        view=result_card(
            "Client generated",
            {
                "package": payload.get("package"),
                "path": payload.get("path"),
                "resources": resource_count,
            },
            tone="success",
        ),
    )


@client_app.command("remove", help="Remove a generated app client.")
def remove_client(
    ctx: typer.Context,
    app: Annotated[str, typer.Argument(help="App slug to remove.")],
    output: Annotated[Path, typer.Option("--output", "-o")] = CLIENT_PACKAGE_ROOT,
) -> None:
    payload = remove_client_package(app=app, output=output)
    emit(
        ctx,
        payload=payload,
        view=notice_card(
            "Client removed",
            f"Removed {payload['app']}.",
            tone="success",
        ),
    )


__all__ = ["client_app"]
