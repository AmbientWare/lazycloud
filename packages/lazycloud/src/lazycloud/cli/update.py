from __future__ import annotations

import subprocess
from typing import Annotated

import typer
from shared.client_version import release_is_newer

from lazycloud.cli.components.cards import notice_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import emit, write_stream
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
        "updated": False,
    }
    if not release_is_newer(latest, current):
        message = (
            f"lazycloud {current} is the latest release."
            if current == latest
            else f"lazycloud {current} is ahead of the latest release, {latest}."
        )
        emit(ctx, payload=payload, view=notice_card("Up to date", message))
        return
    if check:
        emit(
            ctx,
            payload=payload,
            view=notice_card(
                "Update available",
                f"lazycloud {current} is installed; {latest} is the latest release.",
                hint="Run `lazycloud update` to upgrade.",
            ),
        )
        return
    write_stream(f"Upgrading lazycloud {current} -> {latest} with {installation.kind}\n")
    completed = subprocess.run(installation.command, check=False)
    if completed.returncode != 0:
        raise ClientError(
            f"{installation.kind} exited with status {completed.returncode}",
            title="Update failed",
            hint=f"Run it by hand: {' '.join(installation.command)}",
            exit_code=completed.returncode,
        )
    payload["updated"] = True
    emit(
        ctx,
        payload=payload,
        view=notice_card("Updated", f"lazycloud {latest} is installed."),
    )
