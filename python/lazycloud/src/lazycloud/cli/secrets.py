from __future__ import annotations

from typing import Annotated

import typer

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import timestamp
from lazycloud.cli.components.output import (
    emit,
    json_default,
    table,
)
from lazycloud.cli.control import api_session
from lazycloud.clients.api import pages
from lazycloud.contracts.api import Secret, SecretValue

MASKED_SECRET_VALUE = "********"

secret_app = typer.Typer(help="Manage secrets.")


@secret_app.command("list", help="List secret names and update times.")
def secret_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = api_session(workspace=workspace)
    secrets = [
        item
        for page in pages(lambda cursor: client.list_secrets(selected, cursor=cursor, limit=100))
        for item in page.secrets
    ]
    rows = [[item.name, timestamp(item.updated_at)] for item in secrets]
    emit(
        ctx,
        payload=[_secret_payload(item) for item in secrets],
        view=table("Secrets", ["name", "updated"], rows),
    )


@secret_app.command("create", help="Create a secret.")
def secret_create(
    ctx: typer.Context,
    name: str,
    value: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = api_session(workspace=workspace)
    secret = client.set_secret(selected, name, value, exists=False)
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
    client, selected = api_session(workspace=workspace)
    client.set_secret(selected, name, value, exists=True)
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
    client, selected = api_session(workspace=workspace)
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
    client, selected = api_session(workspace=workspace)
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
