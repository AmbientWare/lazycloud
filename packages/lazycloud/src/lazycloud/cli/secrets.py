from __future__ import annotations

from typing import Annotated

import typer
from shared.http.secrets import SecretWireRecord

from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.control import secret_client

MASKED_SECRET_VALUE = "********"

secret_app = typer.Typer(help="Manage secrets.")


@secret_app.command("list")
def secret_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = secret_client(workspace=workspace).list()
    if json_output_enabled(ctx):
        print_payload(ctx, [_secret_payload(item) for item in response.secrets])
        return
    rows = [
        [
            item.name,
            item.updated_at.isoformat(),
            item.created_at.isoformat(),
        ]
        for item in response.secrets
    ]
    console.print(table("Secrets", ["name", "updated", "created"], rows))


@secret_app.command("create")
def secret_create(
    ctx: typer.Context,
    name: str,
    value: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = secret_client(workspace=workspace).create(name, value)
    print_payload(
        ctx,
        {"id": response.id, "name": response.name},
        title="Secret created",
        tone="success",
    )


@secret_app.command("modify")
def secret_modify(
    ctx: typer.Context,
    name: str,
    value: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    secret_client(workspace=workspace).update(name, value)
    print_payload(
        ctx,
        {"name": name, "updated": True},
        title="Secret updated",
        tone="success",
    )


@secret_app.command("delete")
def secret_delete(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    secret_client(workspace=workspace).delete(name)
    print_payload(
        ctx,
        {"name": name, "deleted": True},
        title="Secret deleted",
        tone="success",
    )


@secret_app.command("show")
def secret_show(
    ctx: typer.Context,
    name: str,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Print the secret value."),
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = secret_client(workspace=workspace).get(name)
    if response.secret is None:
        raise typer.BadParameter(f"secret not found: {name}")
    payload = _secret_payload(response.secret, reveal=reveal)
    print_payload(ctx, payload, title="Secret")


def _secret_payload(record: SecretWireRecord, *, reveal: bool = False) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["value"] = record.value if reveal else MASKED_SECRET_VALUE
    return payload


__all__ = ["secret_app"]
