from __future__ import annotations

from typing import Annotated

import typer

from lazycloud._terminal.cards import notice_card
from lazycloud._terminal.formatting import timestamp
from lazycloud._terminal.streams import console
from lazycloud.abstractions.disk import Disk
from lazycloud.cli.components.output import emit, json_output_enabled, print_payload, table
from lazycloud.cli.components.prompts import confirm_destructive
from lazycloud.cli.control import workspace_storage
from lazycloud.contracts import api
from lazycloud.terminal import humanize_bytes

disk_app = typer.Typer(help="Manage durable disks.")


@disk_app.command("list", help="List workspace disks.")
def disk_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    disks = Disk.list(workspace=workspace)
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in disks])
        return
    rows = [
        [
            item.name,
            item.status.value,
            humanize_bytes(item.size_bytes),
            humanize_bytes(item.stored_bytes),
            str(item.generation),
            timestamp(item.updated_at),
            disk_failure(item.failure),
        ]
        for item in disks
    ]
    console.print(
        table(
            "Disks",
            ["name", "status", "size", "stored", "generation", "updated", "last failure"],
            rows,
        )
    )


def disk_failure(failure: api.DiskFailure | None) -> str:
    """Why the holder's last save of a disk failed, or "" since it last succeeded."""
    if failure is None:
        return ""
    return f"{failure.operation.value} at {timestamp(failure.failed_at)}: {failure.message}"


@disk_app.command("delete", help="Delete a disk and everything written to it.")
def disk_delete(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    confirm_destructive(
        ctx,
        subject=f"disk delete `{name}`",
        consequence="Deleting this disk removes everything written to it.",
        yes=yes,
    )
    workspace_storage(workspace=workspace).delete_disk(name)
    emit(
        ctx,
        payload={"name": name, "deleted": True},
        view=notice_card(f"Deleted {name}.", tone="success"),
    )
