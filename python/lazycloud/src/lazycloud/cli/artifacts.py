from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer

from lazycloud.cli.components.output import print_payload
from lazycloud.cli.components.prompts import confirm_destructive
from lazycloud.cli.control import workspace_storage

artifact_app = typer.Typer(help="Browse artifacts, inspect storage usage, and delete files.")


@artifact_app.command("list")
def list_artifacts(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option()] = None,
    task_id: Annotated[str | None, typer.Option()] = None,
    search: Annotated[str, typer.Option()] = "",
    cursor: Annotated[str, typer.Option()] = "",
) -> None:
    page = workspace_storage(workspace=workspace).list_artifacts(
        task_id=_uuid(task_id, "--task-id") if task_id else None,
        search=search or None,
        cursor=cursor or None,
    )
    print_payload(ctx, page.model_dump(mode="json"))


@artifact_app.command("usage")
def artifact_usage(
    ctx: typer.Context, workspace: Annotated[str | None, typer.Option()] = None
) -> None:
    print_payload(
        ctx, workspace_storage(workspace=workspace).get_artifact_summary().model_dump(mode="json")
    )


@artifact_app.command("delete")
def delete_artifact(
    ctx: typer.Context,
    artifact_id: str,
    workspace: Annotated[str | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    selected = _uuid(artifact_id, "ARTIFACT_ID")
    confirm_destructive(
        ctx,
        subject=f"delete artifact {artifact_id}",
        consequence="This permanently removes the artifact.",
        yes=yes,
    )
    workspace_storage(workspace=workspace).delete_artifact(selected)
    print_payload(ctx, {"deleted": artifact_id})


def _uuid(value: str, name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not an id", param_hint=name) from exc
