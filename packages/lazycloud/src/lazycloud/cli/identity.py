from __future__ import annotations

import socket
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Annotated

import typer
from pydantic import JsonValue
from rich.text import Text
from shared.http.device_auth import (
    DeviceCodeCreateResponse,
    DeviceCodeTokenResponse,
)
from shared.http_transport import HttpChannel
from shared.identity import DeviceAuthorizationStatus

from lazycloud.cli.components import theme
from lazycloud.cli.components.output import (
    console,
    error_console,
    json_output_enabled,
    print_payload,
    table,
)
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


def endpoint_url(endpoint: str, *, tls: bool) -> str:
    selected = endpoint.strip()
    if not selected:
        msg = "control plane endpoint must not be empty"
        raise ConfigError(msg)
    if "://" in selected:
        return selected.rstrip("/")
    scheme = "https" if tls else "http"
    return f"{scheme}://{selected}".rstrip("/")


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


def resolve_login_token(
    token: str | None,
    existing: ClientProfile,
) -> tuple[str, str]:
    """Resolve the login credential without requiring a secret command argument.

    An explicit ``--token`` remains authoritative, including an explicitly empty
    value that requests device authorization. Otherwise the canonical
    ``LAZYCLOUD_TOKEN`` setting wins over the stored profile so self-hosted
    bootstrap credentials can enter through the process environment instead of
    argv.
    """
    if token is not None:
        return token, "provided"
    environment_token = settings().token.strip()
    if environment_token:
        return environment_token, "environment"
    return existing.token, "stored"


def device_login_client_name() -> str:
    hostname = socket.gethostname().strip()
    return f"cli@{hostname}" if hostname else "cli"


def device_login(
    endpoint: str,
    *,
    client_name: str,
    announce: Callable[[DeviceCodeCreateResponse], None],
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = DEVICE_LOGIN_TIMEOUT_SECONDS,
) -> DeviceLoginResult:
    """Run the device-code login flow against a control plane.

    Requests a device code, hands the verification details to ``announce``,
    then polls until the user approves in the web app, denies, or the code
    expires. Returns the minted account token and who it belongs to; the token
    reaches every workspace that person is a member of, so the profile keeps
    choosing which one is active.
    """
    channel = HttpChannel(endpoint=endpoint)
    started = DeviceCodeCreateResponse.model_validate(
        _device_request(
            channel,
            "/auth/device",
            {"client_name": client_name},
        )
    )
    announce(started)
    deadline = monotonic() + min(timeout_seconds, float(started.expires_in_seconds))
    interval = float(max(started.poll_interval_seconds, 1))
    while monotonic() < deadline:
        sleep(interval)
        claim = DeviceCodeTokenResponse.model_validate(
            _device_request(
                channel,
                "/auth/device/token",
                {"device_code": started.device_code},
            )
        )
        if claim.status is DeviceAuthorizationStatus.Pending:
            continue
        if claim.status is DeviceAuthorizationStatus.Approved:
            return DeviceLoginResult(token=claim.token)
        msg = f"device login {claim.status.value}"
        raise DeviceLoginError(msg)
    msg = "device login expired before it was approved"
    raise DeviceLoginError(msg)


def _device_request(
    channel: HttpChannel,
    path: str,
    payload: dict[str, JsonValue],
) -> JsonValue:
    try:
        return channel.post(path, payload)
    except urllib.error.URLError as exc:
        msg = f"control plane unreachable at {channel.endpoint}: {exc.reason}"
        raise DeviceLoginError(msg) from exc


def announce_device_login(started: DeviceCodeCreateResponse) -> None:
    error_console.print(
        Text.assemble(
            "To sign in, open ",
            (started.verification_uri_complete, theme.EMPHASIS),
            " and confirm code ",
            (started.user_code, theme.EMPHASIS),
            ".",
        )
    )
    error_console.print(
        f"Waiting for approval (expires in {started.expires_in_seconds // 60} minutes)..."
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
    selected_workspace = workspace or existing.workspace
    selected_tls = tls if tls is not None else existing.tls
    selected_token, token_source = resolve_login_token(token, existing)
    if not selected_token:
        try:
            result = device_login(
                endpoint_url(selected_endpoint, tls=selected_tls),
                client_name=device_login_client_name(),
                announce=announce_device_login,
            )
        except DeviceLoginError as exc:
            error_console.print(theme.styled(str(exc), theme.ERROR))
            raise typer.Exit(1) from exc
        selected_token = result.token
        token_source = "device"

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
    print_payload(
        ctx,
        {
            **profile_payload(saved),
            "activated": activate,
            "token_source": token_source,
        },
    )


@profile_app.command("list")
def profile_list(ctx: typer.Context) -> None:
    profiles = list_profiles()
    active = _active_profile_name_or_default()
    rows = [
        [
            item.name,
            "yes" if item.name == active else "",
            item.endpoint,
            item.workspace,
            "yes" if item.tls else "no",
            "set" if item.token else "",
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
            ["name", "active", "endpoint", "workspace", "tls", "token"],
            rows,
        )
    )


@profile_app.command("current")
def profile_current(ctx: typer.Context) -> None:
    profile = get_profile(apply_env=False)
    print_payload(ctx, {**profile_payload(profile), "active": True})


@profile_app.command("show")
def profile_show(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    selected = get_profile(profile)
    print_payload(ctx, profile_payload(selected))


@profile_app.command("set")
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
    print_payload(ctx, profile_payload(saved))


@profile_app.command("activate")
def profile_activate(ctx: typer.Context, name: str) -> None:
    profile = activate_profile(name)
    print_payload(ctx, f"using profile {profile.name}")


@profile_app.command("delete")
def profile_delete(name: str) -> None:
    delete_profile(name)
    console.print(f"deleted profile {name}")


@token_app.command("set")
def token_set(
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
    console.print("token set")


@token_app.command("show")
def token_show(profile: Annotated[str | None, typer.Option("--profile")] = None) -> None:
    selected = get_profile(profile)
    console.print("set" if selected.token else "")


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
    "endpoint_url",
    "login",
    "profile_app",
    "profile_payload",
    "resolve_login_endpoint",
    "resolve_login_token",
    "token_app",
]
