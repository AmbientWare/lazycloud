from __future__ import annotations

import subprocess
import sys
from typing import Annotated

import typer
from shared.client_version import release_is_newer

from lazycloud.cli.components.cards import notice_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import emit, json_output_active, write_stream
from lazycloud.self_update import (
    SelfUpdateError,
    current_installation,
    installed_version,
    latest_version,
)


def update(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report the installed and latest versions without upgrading."),
    ] = False,
) -> None:
    try:
        installation = current_installation()
        current = installed_version()
        latest = latest_version()
    except SelfUpdateError as exc:
        raise ClientError(str(exc), title="Update unavailable") from exc
    payload: dict[str, str | bool] = {
        "installed": current,
        "latest": latest,
        "installer": str(installation.kind),
        "command": " ".join(installation.command),
        "environment": str(installation.environment),
        "project": str(installation.project) if installation.project else "",
        "updated": False,
    }
    location = f"{installation.kind}: {installation.environment}"
    if not release_is_newer(latest, current):
        message = (
            f"lazycloud {current} is the latest release."
            if current == latest
            else f"lazycloud {current} is ahead of the latest release, {latest}."
        )
        emit(ctx, payload=payload, view=notice_card("Up to date", f"{message}\n{location}"))
        return
    if check:
        emit(
            ctx,
            payload=payload,
            view=notice_card(
                "Update available",
                f"lazycloud {current} is installed; {latest} is the latest release.\n{location}",
                hint="Run `lazycloud update` to upgrade.",
            ),
        )
        return
    write_stream(
        f"Upgrading lazycloud {current} -> {latest}\n{location}\n", error=json_output_active()
    )
    completed = subprocess.run(
        installation.command, check=False, stdout=sys.stderr if json_output_active() else None
    )
    if completed.returncode != 0:
        raise ClientError(
            f"{installation.kind} exited with status {completed.returncode}",
            title="Update failed",
            hint=f"Run it by hand: {' '.join(installation.command)}",
            exit_code=completed.returncode,
        )
    try:
        actual = installation.read_version()
    except SelfUpdateError as exc:
        raise ClientError(str(exc), title="Update verification failed") from exc
    if not release_is_newer(actual, current):
        raise ClientError(
            f"lazycloud remains at {actual} in {installation.environment}.",
            title="No upgrade installed",
            hint=(
                "Check the dependency constraints and sources in your project's pyproject.toml. "
                "An explicit version pin must be changed before it can upgrade."
                if installation.project
                else "Check the installation's version constraints and package index."
            ),
        )
    payload["installed"] = actual
    payload["updated"] = True
    emit(
        ctx,
        payload=payload,
        view=notice_card("Updated", f"lazycloud {actual} is installed.\n{location}"),
    )
