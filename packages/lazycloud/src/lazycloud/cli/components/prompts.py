"""Consistent, automation-safe terminal prompts."""

from __future__ import annotations

import sys

import typer

from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import json_output_enabled


def confirm_destructive(
    ctx: typer.Context,
    *,
    subject: str,
    consequence: str,
    yes: bool,
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
    typer.confirm(f"{consequence} Continue?", abort=True, err=True)


__all__ = ["confirm_destructive"]
