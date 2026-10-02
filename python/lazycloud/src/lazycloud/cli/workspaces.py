from __future__ import annotations

from typing import Annotated, Literal

import typer

from lazycloud._terminal.cards import notice_card
from lazycloud._terminal.streams import console
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import emit, json_output_enabled, print_payload, table
from lazycloud.cli.components.prompts import confirm_destructive
from lazycloud.clients.api import ApiClient, ApiConnectionError, ApiError
from lazycloud.config import ClientProfile, ConfigError, get_profile, set_profile, settings
from lazycloud.contracts.api import WorkspaceState
from lazycloud.control import api_client, resolve_control_client_config

workspace_app = typer.Typer(help="Manage workspaces.")


def workspace_client() -> ApiClient:
    return api_client(resolve_control_client_config())


def _save_workspace(profile: ClientProfile, name: str) -> None:
    stored = get_profile(profile.name, apply_env=False)
    set_profile(
        stored.model_copy(update={"workspace": name}),
        activate=False,
        replace_legacy=True,
    )


def _require_cloud_supported(cloud: str | None) -> Literal["aws"] | None:
    if cloud is None:
        return None
    if cloud != "aws":
        raise ClientError(f"unsupported cloud {cloud!r}; connected clouds: aws")
    return "aws"


def _require_profile_workspace() -> None:
    selected = settings().workspace.strip()
    if not selected:
        return
    raise ClientError(
        f"LAZYCLOUD_WORKSPACE is selecting {selected} instead of the active profile",
        type="workspace_environment_override",
        title="Workspace set by environment",
        hint="Unset LAZYCLOUD_WORKSPACE before changing the profile workspace.",
    )


def _current_workspace(profile: ClientProfile) -> str:
    name = profile.workspace.strip()
    if not name:
        raise ClientError(
            "No workspace is selected.",
            type="workspace_not_selected",
            title="No workspace",
            hint="Run `lazycloud workspace use NAME` first.",
        )
    return name


def _selection_error(
    *,
    title: str,
    message: str,
    workspace: str,
) -> ClientError:
    return ClientError(
        message,
        type="profile_update_failed",
        title=title,
        hint=f"Fix the profile config, then run `lazycloud workspace use {workspace}`.",
    )


@workspace_app.command("list", help="List accessible workspaces.")
def workspace_list(ctx: typer.Context) -> None:
    with workspace_client() as client:
        workspaces = client.list_workspaces()
    if json_output_enabled(ctx):
        print_payload(ctx, {"workspaces": [ws.model_dump(mode="json") for ws in workspaces]})
        return
    current = get_profile().workspace
    rows = [
        [
            workspace.name
            if workspace.state is WorkspaceState.active
            else f"{workspace.name} ({workspace.state.value})",
            "yes" if workspace.name == current else "",
        ]
        for workspace in sorted(workspaces, key=lambda item: item.name)
    ]
    console.print(table("Workspaces", ["name", "current"], rows))


@workspace_app.command(
    "create",
    help="Create and select a workspace. Administrator access required.",
)
def workspace_create(
    ctx: typer.Context,
    name: str,
    cloud: Annotated[
        str | None,
        typer.Option(
            "--cloud",
            help=(
                "Create the workspace in your connected cloud account instead of LazyCloud. "
                "Its compute and volumes live there for good. Accepts: aws."
            ),
        ),
    ] = None,
) -> None:
    _require_profile_workspace()
    location = _require_cloud_supported(cloud)
    profile = get_profile()
    with workspace_client() as client:
        workspace = client.create_workspace(name, cloud=location)
    try:
        _save_workspace(profile, workspace.name)
    except (ConfigError, OSError) as exc:
        raise _selection_error(
            title="Workspace created",
            message=f"Created {workspace.name}, but the CLI could not select it",
            workspace=workspace.name,
        ) from exc
    emit(
        ctx,
        payload=workspace.model_dump(mode="json"),
        view=notice_card(
            f"Using {workspace.name}.",
            title="Workspace created",
            tone="success",
        ),
    )


@workspace_app.command("use", help="Select the workspace used by later commands.")
def workspace_use(ctx: typer.Context, name: str) -> None:
    _require_profile_workspace()
    profile = get_profile()
    with workspace_client() as client:
        workspaces = client.list_workspaces()
    workspace = next((item for item in workspaces if item.name == name), None)
    if workspace is None:
        raise ClientError(
            f"Workspace not found: {name}",
            type="not_found",
            title="Workspace not found",
        )
    try:
        _save_workspace(profile, workspace.name)
    except (ConfigError, OSError) as exc:
        raise _selection_error(
            title="Workspace not selected",
            message=f"Could not select {workspace.name}",
            workspace=workspace.name,
        ) from exc
    payload = workspace.model_dump(mode="json")
    payload["current"] = True
    emit(
        ctx,
        payload=payload,
        view=notice_card(
            f"Using {workspace.name}.",
            tone="success",
        ),
    )


@workspace_app.command("rename", help="Rename the selected workspace.")
def workspace_rename(ctx: typer.Context, name: str) -> None:
    _require_profile_workspace()
    profile = get_profile()
    with workspace_client() as client:
        workspace = client.rename_workspace(_current_workspace(profile), name)
    try:
        _save_workspace(profile, workspace.name)
    except (ConfigError, OSError) as exc:
        raise _selection_error(
            title="Workspace renamed",
            message=f"Renamed the workspace to {workspace.name}, but the CLI profile is unchanged",
            workspace=workspace.name,
        ) from exc
    emit(
        ctx,
        payload=workspace.model_dump(mode="json"),
        view=notice_card(
            f"Using {workspace.name}.",
            title="Workspace renamed",
            tone="success",
        ),
    )


@workspace_app.command(
    "delete",
    help="Permanently delete a workspace. Administrator access required.",
)
def workspace_delete(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    profile = get_profile()
    current = profile.workspace
    if current == name:
        _require_profile_workspace()
    confirm_destructive(
        ctx,
        subject=f"Delete workspace {name}?",
        consequence=(
            "This permanently deletes its access tokens, configuration, and owned resources."
        ),
        confirmation=name,
        confirmation_label="Workspace name",
        yes=yes,
    )
    with workspace_client() as client:
        deleted = client.delete_workspace(name)
        fallback = ""
        if current == name:
            recovery_workspace = "default"
            try:
                remaining = sorted(
                    (
                        item
                        for item in client.list_workspaces()
                        if item.state is WorkspaceState.active and item.name != name
                    ),
                    key=lambda item: item.name,
                )
                workspace = next((item for item in remaining if item.name == "default"), None)
                workspace = workspace or (remaining[0] if remaining else None)
                if workspace is None:
                    raise ValueError("no active workspace remains")
                fallback = workspace.name
                recovery_workspace = fallback
                _save_workspace(profile, fallback)
            except (ConfigError, ApiError, ApiConnectionError, OSError, ValueError) as exc:
                raise _selection_error(
                    title="Workspace deleted",
                    message=f"Deleted {name}, but the CLI could not select a remaining workspace",
                    workspace=recovery_workspace,
                ) from exc
    emit(
        ctx,
        payload={
            "name": name,
            "deleted": True,
            "state": deleted.state.value,
            "current": fallback or current,
        },
        view=notice_card(
            f"Deleted {name}; its data is removed in the background."
            + (f" Using {fallback}." if fallback else ""),
            tone="success",
        ),
    )


__all__ = [
    "workspace_app",
    "workspace_client",
    "workspace_create",
    "workspace_delete",
    "workspace_list",
    "workspace_rename",
    "workspace_use",
]
