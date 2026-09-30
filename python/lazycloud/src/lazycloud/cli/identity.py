from __future__ import annotations

from typing import Annotated

import typer
from shared.api import Me

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.streams import console
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import emit, json_output_enabled, print_payload, table
from lazycloud.clients.api import ApiClient
from lazycloud.config import (
    DEFAULT_PROFILE,
    ClientProfile,
    ConfigError,
    activate_profile,
    active_profile_name,
    delete_profile,
    get_profile,
    list_profiles,
    set_profile,
    settings,
)
from lazycloud.control import endpoint_url

profile_app = typer.Typer(help="Manage client profiles.")
token_app = typer.Typer(help="Manage access tokens.")


def resolve_login_endpoint(endpoint: str | None, existing: ClientProfile) -> str:
    """Resolve the endpoint a login must use.

    Resolution order: ``--endpoint`` flag, ``LAZYCLOUD_ENDPOINT`` env, the
    endpoint stored in the profile, then the packaged hosted default. Plain
    ``lazycloud login`` with nothing configured targets the hosted platform;
    self-hosters and local dev point at their own stack via flag or env.
    """
    flag = (endpoint or "").strip()
    if flag:
        return flag
    env_endpoint = settings().endpoint.strip()
    if env_endpoint:
        return env_endpoint
    return existing.resolved_endpoint()


def resolve_login_token(token: str | None) -> str:
    """The credential to log in with: ``--token``, then ``LAZYCLOUD_TOKEN``."""
    selected = (token if token is not None else settings().token).strip()
    if not selected:
        raise ClientError(
            "No access token was given.",
            type="login_failed",
            title="Login failed",
            hint="Pass --token or set LAZYCLOUD_TOKEN.",
        )
    return selected


def select_workspace(me: Me, requested: str) -> str:
    """The workspace the profile will use, checked against what the token reaches."""
    names = [workspace.name for workspace in me.workspaces]
    if requested:
        if requested not in names:
            reachable = ", ".join(names) or "none"
            raise ClientError(
                f"The token does not reach workspace {requested}.",
                type="login_failed",
                title="Login failed",
                hint=f"Workspaces this token reaches: {reachable}.",
            )
        return requested
    if len(names) == 1:
        return names[0]
    raise ClientError(
        "Choose a workspace for this profile.",
        type="login_failed",
        title="Login failed",
        hint=f"Pass --workspace with one of: {', '.join(names) or 'none'}.",
    )


def login(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    endpoint: Annotated[
        str | None,
        typer.Option(
            "--endpoint",
            help=(
                "Control plane endpoint URL for self-hosting or local dev. Omit to "
                "use LAZYCLOUD_ENDPOINT, your stored profile, or the hosted default."
            ),
        ),
    ] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Access token to store.")] = None,
    tls: Annotated[bool | None, typer.Option("--tls/--no-tls")] = None,
    activate: Annotated[bool, typer.Option("--activate/--no-activate")] = True,
) -> None:
    profile_name = _target_profile_name(profile)
    existing = _stored_profile_or_default(profile_name)
    selected_endpoint = resolve_login_endpoint(endpoint, existing)
    selected_tls = tls if tls is not None else existing.tls
    selected_token = resolve_login_token(token)
    selected_endpoint_url = endpoint_url(selected_endpoint, tls=selected_tls)
    with ApiClient(endpoint=selected_endpoint_url, token=selected_token) as client:
        me = client.me()
    selected_workspace = select_workspace(me, workspace or existing.workspace)
    saved = set_profile(
        ClientProfile(
            name=profile_name,
            endpoint=selected_endpoint,
            workspace=selected_workspace,
            token=selected_token,
            tls=selected_tls,
        ),
        activate=activate,
        replace_legacy=True,
    )
    payload: dict[str, object] = {
        **profile_payload(saved),
        "activated": activate,
        "user": me.user.email,
    }
    emit(
        ctx,
        payload=payload,
        view=notice_card(
            f"Signed in as {me.user.email} in workspace {selected_workspace} "
            f"at {selected_endpoint_url}."
            if activate
            else (
                f"Saved profile {saved.name} for {selected_endpoint_url} without making it active."
            ),
            title="Signed in" if activate else "Profile saved",
            hint=("" if activate else f"Run `lazycloud profile activate {saved.name}` to use it."),
            tone="success",
        ),
    )


@profile_app.command("list", help="List configured client profiles.")
def profile_list(ctx: typer.Context) -> None:
    profiles = list_profiles()
    active = _active_profile_name_or_default()
    rows = [
        [
            item.name,
            item.resolved_endpoint(),
            "yes" if item.name == active else "",
        ]
        for item in profiles
    ]
    if json_output_enabled(ctx):
        print_payload(
            ctx,
            [{**profile_payload(item), "active": item.name == active} for item in profiles],
        )
        return
    console.print(
        table(
            "Profiles",
            ["name", "endpoint", "active"],
            rows,
        )
    )


@profile_app.command("current", help="Show the active client profile.")
def profile_current(ctx: typer.Context) -> None:
    profile = get_profile(apply_env=False)
    payload: dict[str, object] = {**profile_payload(profile), "active": True}
    emit(
        ctx,
        payload=payload,
        view=result_card(
            {
                "name": profile.name,
                "endpoint": profile.resolved_endpoint(),
                "workspace": profile.workspace,
            },
        ),
    )


@profile_app.command("show", help="Show a client profile.")
def profile_show(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    selected = get_profile(profile)
    emit(
        ctx,
        payload=profile_payload(selected),
        view=result_card(
            {
                "name": selected.name,
                "endpoint": selected.resolved_endpoint(),
                "workspace": selected.workspace,
            },
        ),
    )


@profile_app.command("set", help="Create or update a client profile.")
def profile_set(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    token: Annotated[str | None, typer.Option("--token")] = None,
    tls: Annotated[bool | None, typer.Option("--tls/--no-tls")] = None,
    activate: Annotated[bool, typer.Option("--activate/--no-activate")] = True,
) -> None:
    profile_name = _target_profile_name(profile)
    existing = _stored_profile_or_default(profile_name)
    updates: dict[str, object] = {}
    if endpoint is not None:
        updates["endpoint"] = endpoint
    if workspace is not None:
        updates["workspace"] = workspace
    if token is not None:
        updates["token"] = token
    if tls is not None:
        updates["tls"] = tls
    saved = set_profile(
        existing.model_copy(update=updates),
        activate=activate,
        replace_legacy=True,
    )
    payload = profile_payload(saved)
    emit(
        ctx,
        payload=payload,
        view=notice_card(
            f"Profile {saved.name} is active."
            if activate
            else f"Saved profile {saved.name} without making it active.",
            tone="success",
        ),
    )


@profile_app.command("activate", help="Make a profile active.")
def profile_activate(ctx: typer.Context, name: str) -> None:
    profile = activate_profile(name)
    payload: dict[str, object] = {**profile_payload(profile), "active": True}
    emit(
        ctx,
        payload=payload,
        view=notice_card(
            f"Using profile {profile.name}.",
            tone="success",
        ),
    )


@profile_app.command("delete", help="Delete a client profile.")
def profile_delete(ctx: typer.Context, name: str) -> None:
    delete_profile(name)
    emit(
        ctx,
        payload={"name": name, "deleted": True},
        view=notice_card(
            f"Deleted profile {name}.",
            tone="success",
        ),
    )


@token_app.command("set", help="Save an access token for a profile.")
def token_set(
    ctx: typer.Context,
    value: str,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    profile_name = _target_profile_name(profile)
    selected = _stored_profile_or_default(profile_name)
    set_profile(
        selected.model_copy(update={"token": value}),
        activate=profile_name == _active_profile_name_or_default(),
        replace_legacy=True,
    )
    emit(
        ctx,
        payload={"profile": profile_name, "token": "set"},
        view=notice_card(
            f"Saved the token for profile {profile_name}.",
            tone="success",
        ),
    )


@token_app.command("show", help="Show whether a profile has an access token.")
def token_show(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    selected = get_profile(profile)
    emit(
        ctx,
        payload={"profile": selected.name, "token": "set" if selected.token else "not set"},
        view=notice_card(
            f"Profile {selected.name} has "
            f"{'a saved token.' if selected.token else 'no saved token.'}",
            tone="neutral",
        ),
    )


def _target_profile_name(name: str | None) -> str:
    if name:
        return name
    configured = settings().profile.strip()
    if configured:
        return configured
    return _active_profile_name_or_default()


def _active_profile_name_or_default() -> str:
    try:
        return active_profile_name()
    except ConfigError:
        return DEFAULT_PROFILE


def _stored_profile_or_default(name: str) -> ClientProfile:
    try:
        return get_profile(name, apply_env=False)
    except (ConfigError, KeyError):
        return ClientProfile(name=name)


def profile_payload(profile: ClientProfile, *, include_token: bool = False) -> dict[str, object]:
    payload = profile.model_dump(mode="json")
    payload["token"] = profile.token if include_token else ("set" if profile.token else "")
    return payload


__all__ = [
    "login",
    "profile_app",
    "profile_payload",
    "resolve_login_endpoint",
    "resolve_login_token",
    "select_workspace",
    "token_app",
]
