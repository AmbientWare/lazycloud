from __future__ import annotations

from typing import Annotated

import typer
from shared.http.errors import HttpApiError, HttpTransportError
from shared.identity import WorkspaceStatus

from lazycloud.cli.components.cards import notice_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import (
    console,
    emit,
    json_output_enabled,
    print_payload,
    table,
)
from lazycloud.cli.components.prompts import confirm_destructive
from lazycloud.cli.control import workspace_client
from lazycloud.config import ClientProfile, ConfigError, get_profile, set_profile, settings

workspace_app = typer.Typer(help="Manage workspaces.")


def _save_workspace(profile: ClientProfile, name: str) -> None:
    stored = get_profile(profile.name, apply_env=False)
    set_profile(
        stored.model_copy(update={"workspace": name}),
        activate=False,
        replace_legacy=True,
    )


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
    client = workspace_client()
    response = client.list()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    current = client.current()
    rows = [
        [workspace.name, "yes" if workspace.id == current.id else ""]
        for workspace in sorted(response.workspaces, key=lambda item: item.name)
    ]
    console.print(table("Workspaces", ["name", "current"], rows, expand=False))


@workspace_app.command(
    "create",
    help="Create and select a workspace. Administrator access required.",
)
def workspace_create(ctx: typer.Context, name: str) -> None:
    _require_profile_workspace()
    profile = get_profile()
    workspace = workspace_client().create(name)
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
            "Workspace created",
            f"Using {workspace.name}.",
            tone="success",
        ),
    )


@workspace_app.command("use", help="Select the workspace used by later commands.")
def workspace_use(ctx: typer.Context, name: str) -> None:
    _require_profile_workspace()
    profile = get_profile()
    workspaces = workspace_client().list().workspaces
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
            "Workspace selected",
            f"Using {workspace.name}.",
            tone="success",
        ),
    )


@workspace_app.command("rename", help="Rename the selected workspace.")
def workspace_rename(ctx: typer.Context, name: str) -> None:
    _require_profile_workspace()
    profile = get_profile()
    workspace = workspace_client().rename(name)
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
            "Workspace renamed",
            f"Using {workspace.name}.",
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
    client = workspace_client()
    current = client.current()
    if current.name == name:
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
    client.delete(name)
    fallback = ""
    if current.name == name:
        recovery_workspace = "default"
        try:
            remaining = sorted(
                (
                    item
                    for item in client.list().workspaces
                    if item.status is WorkspaceStatus.Active
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
        except (ConfigError, HttpApiError, HttpTransportError, OSError, ValueError) as exc:
            raise _selection_error(
                title="Workspace deleted",
                message=f"Deleted {name}, but the CLI could not select a remaining workspace",
                workspace=recovery_workspace,
            ) from exc
    emit(
        ctx,
        payload={"name": name, "deleted": True, "current": fallback or current.name},
        view=notice_card(
            "Workspace deleted",
            f"Deleted {name}." + (f" Using {fallback}." if fallback else ""),
            tone="success",
        ),
    )


__all__ = [
    "workspace_app",
    "workspace_create",
    "workspace_delete",
    "workspace_list",
    "workspace_rename",
    "workspace_use",
]
