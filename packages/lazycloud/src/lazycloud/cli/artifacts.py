from __future__ import annotations

from typing import Annotated

import typer

from lazycloud.cli.components.output import print_payload
from lazycloud.cli.components.prompts import confirm_destructive
from lazycloud.cli.control import control_config
from lazycloud.clients.artifact.control import ArtifactControlClient

artifact_app = typer.Typer(help="Browse artifacts, inspect storage usage, and delete files.")


def _client(workspace: str | None) -> ArtifactControlClient:
    config = control_config(workspace=workspace)
    return ArtifactControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        workspace=config.workspace,
        timeout_seconds=config.timeout_seconds,
    )


@artifact_app.command("list")
def list_artifacts(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option()] = None,
    task_id: Annotated[str | None, typer.Option()] = None,
    search: Annotated[str, typer.Option()] = "",
    cursor: Annotated[str, typer.Option()] = "",
) -> None:
    print_payload(
        ctx,
        _client(workspace)
        .list(task_id=task_id, search=search, cursor=cursor)
        .model_dump(mode="json"),
    )


@artifact_app.command("usage")
def artifact_usage(
    ctx: typer.Context, workspace: Annotated[str | None, typer.Option()] = None
) -> None:
    print_payload(ctx, _client(workspace).summary().model_dump(mode="json"))


@artifact_app.command("delete")
def delete_artifact(
    ctx: typer.Context,
    artifact_id: str,
    workspace: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    confirm_destructive(
        ctx,
        subject=f"delete artifact {artifact_id}",
        consequence="This permanently removes the artifact and settles its storage charges.",
        yes=yes,
    )
    _client(workspace).delete(artifact_id)
    print_payload(ctx, {"deleted": artifact_id})
