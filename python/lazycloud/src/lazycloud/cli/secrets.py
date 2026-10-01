from __future__ import annotations

from typing import Annotated

import typer
from shared.api import Secret, SecretValue

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import timestamp
from lazycloud._terminal.streams import console
from lazycloud.cli.components.output import (
    emit,
    json_default,
    json_output_enabled,
    print_payload,
    table,
)
from lazycloud.clients.api import ApiClient
from lazycloud.control import api_client, require_workspace, resolve_control_client_config

MASKED_SECRET_VALUE = "********"

secret_app = typer.Typer(help="Manage secrets.")


def _session(workspace: str | None) -> tuple[ApiClient, str]:
    config = resolve_control_client_config(workspace=workspace)
    return api_client(config), require_workspace(config)


@secret_app.command("list", help="List secret names and update times.")
def secret_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    secrets: list[Secret] = []
    cursor: str | None = None
    while True:
        page = client.list_secrets(selected, cursor=cursor, limit=100)
        secrets.extend(page.secrets)
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    if json_output_enabled(ctx):
        print_payload(ctx, [_secret_payload(item) for item in secrets])
        return
    rows = [[item.name, timestamp(item.updated_at)] for item in secrets]
    console.print(table("Secrets", ["name", "updated"], rows))


@secret_app.command("create", help="Create a secret.")
def secret_create(
    ctx: typer.Context,
    name: str,
    value: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    secret = client.create_secret(selected, name, value)
    emit(
        ctx,
        payload={"id": secret.name, "name": secret.name},
        view=notice_card(f"Created {secret.name}.", tone="success"),
    )


@secret_app.command("modify", help="Replace a secret value.")
def secret_modify(
    ctx: typer.Context,
    name: str,
    value: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    client.update_secret(selected, name, value)
    emit(
        ctx,
        payload={"name": name, "updated": True},
        view=notice_card(f"Updated {name}.", tone="success"),
    )


@secret_app.command("delete", help="Delete a secret.")
def secret_delete(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    client.delete_secret(selected, name)
    emit(
        ctx,
        payload={"name": name, "deleted": True},
        view=notice_card(f"Deleted {name}.", tone="success"),
    )


@secret_app.command("show", help="Show a secret, masked unless explicitly revealed.")
def secret_show(
    ctx: typer.Context,
    name: str,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Print the secret value."),
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    # The value is fetched only when asked for, so a masked show never
    # transfers it.
    secret: Secret | SecretValue
    if reveal:
        secret = client.get_secret_value(selected, name)
        payload = secret.model_dump(mode="json")
    else:
        secret = client.get_secret(selected, name)
        payload = _secret_payload(secret)
    emit(
        ctx,
        payload=payload,
        view=result_card(
            json_default(
                {
                    "name": secret.name,
                    "value": payload["value"],
                    "updated": timestamp(secret.updated_at),
                }
            ),
        ),
    )


def _secret_payload(secret: Secret, *, value: str = MASKED_SECRET_VALUE) -> dict[str, object]:
    payload: dict[str, object] = secret.model_dump(mode="json")
    payload["value"] = value
    return payload


__all__ = ["secret_app"]
