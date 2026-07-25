from __future__ import annotations

from typing import Annotated

import typer
from shared.http.errors import HttpApiError
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
    try:
        response = secret_client(workspace=workspace).create(name, value)
    except HttpApiError as exc:
        raise typer.BadParameter(exc.detail or "secret create failed") from exc
    print_payload(ctx, {"id": response.id, "name": response.name})


@secret_app.command("modify")
def secret_modify(
    ctx: typer.Context,
    name: str,
    value: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    try:
        secret_client(workspace=workspace).update(name, value)
    except HttpApiError as exc:
        raise typer.BadParameter(exc.detail or "secret modify failed") from exc
    print_payload(ctx, {"name": name})


@secret_app.command("delete")
def secret_delete(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    try:
        secret_client(workspace=workspace).delete(name)
    except HttpApiError as exc:
        raise typer.BadParameter(exc.detail or "secret delete failed") from exc
    print_payload(ctx, {"name": name})


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
    try:
        response = secret_client(workspace=workspace).get(name)
    except HttpApiError as exc:
        raise typer.BadParameter(exc.detail or f"secret not found: {name}") from exc
    if response.secret is None:
        raise typer.BadParameter(f"secret not found: {name}")
    payload = _secret_payload(response.secret, reveal=reveal)
    if json_output_enabled(ctx):
        print_payload(ctx, payload)
        return
    console.print(table("Secret", ["name", "value"], [[payload["name"], payload["value"]]]))


def _secret_payload(record: SecretWireRecord, *, reveal: bool = False) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["value"] = record.value if reveal else MASKED_SECRET_VALUE
    return payload


__all__ = ["secret_app"]
