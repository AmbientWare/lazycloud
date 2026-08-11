from __future__ import annotations

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
from pydantic import JsonValue
from shared.http.system import TokenCreateRequest
from shared.http.users import UserCreateRequest, UserRoleRequest
from shared.http_transport import HttpChannel
from shared.identity import PlatformRole

from cli.api_client import AdminApiClient, admin_api_client


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
    name: Annotated[str, typer.Option("--name", help="Display name for the account.")] = "",
    github_user_id: Annotated[
        int | None,
        typer.Option(
            "--github-user-id",
            help=(
                "Numeric GitHub user id that may sign in as this account. Resolve it with "
                "curl -s https://api.github.com/users/<login>. Without it the account "
                "cannot sign in and exists only to own tokens."
            ),
        ),
    ] = None,
    github_login: Annotated[str, typer.Option("--github-login")] = "",
    administrator: Annotated[bool, typer.Option("--administrator")] = False,
) -> None:
    """Create an account, optionally pre-linked to the GitHub identity that reaches it."""
    response = admin_api_client().create_user(
        UserCreateRequest(
            display_name=name,
            github_user_id=github_user_id,
            github_login=github_login,
            role=PlatformRole.Administrator if administrator else PlatformRole.Member,
        )
    )
    print_payload(ctx, response.model_dump(mode="json"))


def user_set_role(
    ctx: typer.Context,
    user_id: str,
    administrator: Annotated[
        bool,
        typer.Option(
            "--administrator/--member",
            help="Grant or withdraw platform administrator standing.",
        ),
    ] = True,
) -> None:
    """Change an existing account's platform role.

    Signing in makes an ordinary member, so this is how somebody who already has
    an account becomes an administrator.
    """
    response = admin_api_client().set_user_role(
        user_id,
        UserRoleRequest(role=PlatformRole.Administrator if administrator else PlatformRole.Member),
    )
    print_payload(ctx, response.model_dump(mode="json"))


def user_list(ctx: typer.Context) -> None:
    users = admin_api_client().list_users().data
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in users])
        return
    rows = [
        [item.id, item.display_name, item.github_login, item.role.value, item.status.value]
        for item in users
    ]
    console.print(table("Accounts", ["id", "name", "github", "role", "status"], rows))


def token_create(
    ctx: typer.Context,
    name: str,
    expires_in: Annotated[int | None, typer.Option("--expires-in")] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Mint for this account instead of the acting one."),
    ] = None,
) -> None:
    """Mint a credential reaching every workspace its owning account holds.

    With --user the credential belongs to that account rather than to the
    administrator who ran this, so what it does stays attributable to them. It is
    the only way an account with no GitHub identity gets a first credential.
    """
    request = TokenCreateRequest(name=name, expires_in_seconds=expires_in)
    client = admin_api_client()
    response = (
        client.create_token_for_user(user, request)
        if user is not None
        else client.create_token(request)
    )
    print_payload(ctx, response.model_dump(mode="json"))


def token_list(ctx: typer.Context) -> None:
    tokens = admin_api_client().list_tokens().data
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
user_app.command("list")(user_list)
user_app.command("set-role")(user_set_role)
