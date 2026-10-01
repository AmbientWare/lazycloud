from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Annotated

import typer
from shared.api import (
    DeviceLogin,
    DeviceTokenStatus,
    Me,
    WorkspaceRole,
    WorkspaceState,
)

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.streams import console, error_console
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import emit, json_output_enabled, print_payload, table
from lazycloud.clients.api import ApiClient, ApiConnectionError, ApiError
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

DEVICE_LOGIN_TIMEOUT_SECONDS = 900.0


class DeviceLoginError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceLoginResult:
    # The minted token is excluded from the repr so a traceback or a logged
    # result never carries the credential the device flow just issued.
    token: str = field(repr=False)


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


def resolve_login_token(token: str | None) -> tuple[str, str]:
    """Resolve the login credential without requiring a secret command argument.

    An explicit ``--token`` remains authoritative, including an empty value that
    requests device authorization. Otherwise ``LAZYCLOUD_TOKEN`` supplies a
    non-interactive credential without exposing it in process arguments. With
    neither source, login starts device authorization. The stored token remains
    untouched until the new credential works.
    """
    if token is not None:
        return token.strip(), "provided"
    environment_token = settings().token.strip()
    if environment_token:
        return environment_token, "environment"
    return "", "device"


def device_login_client_name() -> str:
    hostname = socket.gethostname().strip()
    return f"cli@{hostname}" if hostname else "cli"


def device_login(
    endpoint: str,
    *,
    client_name: str,
    announce: Callable[[DeviceLogin], None],
    sleep: Callable[[float], None] | None = None,
    timeout_seconds: float = DEVICE_LOGIN_TIMEOUT_SECONDS,
) -> DeviceLoginResult:
    """Run the device-code login flow against a control plane.

    Requests a device code, hands the verification details to ``announce``,
    then polls until the user approves in the web app, denies, or the code
    expires. A ``slow_down`` answer lengthens the wait between polls. Returns
    the minted account token; it reaches every workspace that person is a
    member of, so the profile keeps choosing which one is active.
    """
    with ApiClient(endpoint=endpoint, token="") as client:
        try:
            started = client.start_device_login(client_name)
            announce(started)
            deadline = monotonic() + min(timeout_seconds, float(started.expires_in_seconds))
            interval = float(max(started.poll_interval_seconds, 1))
            while monotonic() < deadline:
                (sleep or time.sleep)(interval)
                claim = client.poll_device_login(started.device_code)
                interval = float(max(claim.poll_interval_seconds, 1))
                if claim.status in (DeviceTokenStatus.pending, DeviceTokenStatus.slow_down):
                    continue
                if claim.status is DeviceTokenStatus.approved and claim.token:
                    return DeviceLoginResult(token=claim.token)
                msg = (
                    "The sign-in request was denied."
                    if claim.status is DeviceTokenStatus.denied
                    else "The sign-in code expired."
                )
                raise DeviceLoginError(msg)
        except ApiConnectionError as exc:
            msg = f"Could not reach {endpoint}: {exc.reason}"
            raise DeviceLoginError(msg) from exc
    msg = "The sign-in code expired."
    raise DeviceLoginError(msg)


def announce_device_login(
    started: DeviceLogin,
    *,
    profile: str,
    endpoint: str,
) -> None:
    # The link is minted by whichever control plane the profile points at, so
    # the card says which one that is: a stale local profile is otherwise only
    # visible in the link's host.
    error_console.print(
        notice_card(
            f"Profile {profile} at {endpoint}\n\nOpen {started.verification_uri_complete}",
            title="Sign in to lazycloud",
            hint=(
                f"Confirm code {started.user_code}. "
                f"It expires in {started.expires_in_seconds // 60} minutes. "
                "Wrong control plane? Pass --endpoint or activate another profile."
            ),
        )
    )


def select_workspace(client: ApiClient, me: Me, requested: str) -> str:
    """The workspace the profile will use.

    A named workspace must be one the token reaches; administrators reach
    workspaces they are not members of. Otherwise the account's own
    workspace, the one it owns and made first, or its only one.
    """
    active = [ws for ws in me.workspaces if ws.state is WorkspaceState.active]
    names = [ws.name for ws in active]
    if requested:
        if requested in names:
            return requested
        try:
            client.get_workspace(requested)
        except ApiError as exc:
            if exc.status_code not in (403, 404):
                raise
            reachable = ", ".join(names) or "none"
            raise ClientError(
                f"The token does not reach workspace {requested}.",
                type="login_failed",
                title="Login failed",
                hint=f"Workspaces this token reaches: {reachable}.",
            ) from exc
        return requested
    owned = sorted(
        (ws for ws in active if ws.role is WorkspaceRole.owner), key=lambda ws: ws.created_at
    )
    if owned:
        return owned[0].name
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
    token: Annotated[str | None, typer.Option("--token")] = None,
    tls: Annotated[bool | None, typer.Option("--tls/--no-tls")] = None,
    activate: Annotated[bool, typer.Option("--activate/--no-activate")] = True,
) -> None:
    profile_name = _target_profile_name(profile)
    existing = _stored_profile_or_default(profile_name)
    selected_endpoint = resolve_login_endpoint(endpoint, existing)
    selected_tls = tls if tls is not None else existing.tls
    selected_token, token_source = resolve_login_token(token)
    selected_endpoint_url = endpoint_url(selected_endpoint, tls=selected_tls)
    if not selected_token:
        try:
            result = device_login(
                selected_endpoint_url,
                client_name=device_login_client_name(),
                announce=lambda started: announce_device_login(
                    started,
                    profile=profile_name,
                    endpoint=selected_endpoint_url,
                ),
            )
        except DeviceLoginError as exc:
            raise ClientError(
                str(exc),
                type="login_failed",
                title="Login failed",
            ) from exc
        selected_token = result.token
        token_source = "device"

    with ApiClient(endpoint=selected_endpoint_url, token=selected_token) as client:
        me = client.me()
        selected_workspace = select_workspace(client, me, workspace or existing.workspace)
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
        "token_source": token_source,
    }
    emit(
        ctx,
        payload=payload,
        view=notice_card(
            f"Profile {saved.name} is active at {selected_endpoint_url}."
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
    "DeviceLoginError",
    "DeviceLoginResult",
    "announce_device_login",
    "device_login",
    "device_login_client_name",
    "login",
    "profile_app",
    "profile_payload",
    "resolve_login_endpoint",
    "resolve_login_token",
    "select_workspace",
    "token_app",
]
