from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from lazycloud.cli.components.output import (
    console,
    json_output_enabled,
    print_payload,
    table,
)
from lazycloud.cli.identity import profile_payload
from lazycloud.config import (
    get_profile,
)
from lazycloud.json_contracts import validate_json_object
from pydantic import JsonValue, SecretStr
from shared.http.system import TokenCreateRequest
from shared.http.users import UserCreateRequest
from shared.http_transport import HttpChannel
from shared.identity import PlatformRole

from cli.api_client import AdminApiClient, admin_api_client
from cli.offline_auth import read_private_file


def profile_export(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    include_token: Annotated[bool, typer.Option("--include-token")] = False,
) -> None:
    selected = get_profile(profile, apply_env=False)
    export = AdminApiClient(
        channel=HttpChannel(
            endpoint=selected.resolved_endpoint(),
            token=selected.token or None,
            timeout_seconds=10.0,
        ),
        workspace=selected.workspace,
    ).export_workspace()
    exported_profile = validate_json_object(
        selected.model_dump(mode="json", exclude={"name", "token"})
    )
    if include_token:
        exported_profile["token"] = selected.token
    payload: dict[str, JsonValue] = {
        **validate_json_object(export.model_dump(mode="json")),
        "active_profile": selected.name,
        "api_url": selected.endpoint,
        "token": selected.token if include_token else ("set" if selected.token else ""),
        "profile": validate_json_object(profile_payload(selected, include_token=include_token)),
        "config": {
            "active_profile": selected.name,
            "profiles": {
                selected.name: exported_profile,
            },
        },
    }
    print_payload(ctx, payload)


user_app = typer.Typer(help="Manage accounts.")


def user_create(
    ctx: typer.Context,
    username: Annotated[str, typer.Option("--username")],
    password_file: Annotated[
        Path,
        typer.Option(
            "--password-file",
            dir_okay=False,
            resolve_path=True,
            help="Private file holding the new account's password.",
        ),
    ],
    administrator: Annotated[bool, typer.Option("--administrator")] = False,
) -> None:
    """Create an account. The password is read from a file, never from argv."""
    password = read_private_file(password_file, description="password file")
    response = admin_api_client().create_user(
        UserCreateRequest(
            username=username,
            password=SecretStr(password),
            role=PlatformRole.Administrator if administrator else PlatformRole.Member,
        )
    )
    print_payload(ctx, response.model_dump(mode="json"))


def token_create(
    ctx: typer.Context,
    name: str,
    expires_in: Annotated[int | None, typer.Option("--expires-in")] = None,
) -> None:
    """Mint a credential for the acting account, reaching every workspace it holds."""
    response = admin_api_client().create_token(
        TokenCreateRequest(name=name, expires_in_seconds=expires_in)
    )
    print_payload(ctx, response.model_dump(mode="json"))


def token_list(ctx: typer.Context) -> None:
    tokens = admin_api_client().list_tokens().tokens
    payload: list[dict[str, str | bool | list[str] | None]] = [
        {
            "id": item.id,
            "name": item.name,
            "prefix": item.prefix,
            "kind": item.kind.value,
            "workspace_id": item.workspace_id,
            "status": item.status.value,
            "scopes": item.scopes,
            "reusable": item.reusable,
            "created_at": item.created_at.isoformat(),
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        }
        for item in tokens
    ]
    if json_output_enabled(ctx):
        print_payload(ctx, payload)
    else:
        rows = [
            [
                item.id,
                item.name,
                item.kind.value,
                item.workspace_id,
                item.status.value,
                ",".join(item.scopes),
            ]
            for item in tokens
        ]
        console.print(
            table("Tokens", ["id", "name", "kind", "workspace", "status", "scopes"], rows)
        )


def token_revoke(ctx: typer.Context, token_id_or_name: str) -> None:
    """End a credential the acting account holds."""
    record = admin_api_client().revoke_token(token_id_or_name)
    print_payload(ctx, record.model_dump(mode="json"))


user_app.command("create")(user_create)
