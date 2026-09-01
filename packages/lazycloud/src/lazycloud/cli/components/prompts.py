"""Consistent, automation-safe terminal prompts."""

from __future__ import annotations

import sys

import typer

from lazycloud.cli.components.cards import notice_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import error_console, json_output_enabled


def confirm_destructive(
    ctx: typer.Context,
    *,
    subject: str,
    consequence: str,
    yes: bool,
    confirmation: str | None = None,
    confirmation_label: str = "Confirmation",
) -> None:
    if yes:
        return
    hint = "Pass --yes to confirm without a prompt."
    if json_output_enabled(ctx):
        raise ClientError(
            f"{subject} requires confirmation in JSON mode",
            type="confirmation_required",
            title="Confirmation required",
            hint=hint,
        )
    if not sys.stdin.isatty():
        raise ClientError(
            f"{subject} requires confirmation, and no interactive terminal is attached",
            type="confirmation_required",
            title="Confirmation required",
            hint=hint,
        )
    if confirmation is None:
        typer.confirm(f"{consequence} Continue?", abort=True, err=True)
        return
    error_console.print(
        notice_card(
            subject,
            consequence,
            hint=f"Enter {confirmation} to continue.",
            tone="warning",
        )
    )
    entered = typer.prompt(confirmation_label, err=True)
    if entered == confirmation:
        return
    raise ClientError(
        "The confirmation did not match.",
        type="confirmation_mismatch",
        title="Confirmation did not match",
    )


__all__ = ["confirm_destructive"]
