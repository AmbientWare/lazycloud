"""One execution and error boundary for both CLI applications."""

from __future__ import annotations

import typer

from lazycloud.cli.components.errors import (
    CliErrorPolicy,
    debug_errors_enabled,
    json_errors_enabled,
    render_exception,
)
from lazycloud.cli.components.output import json_output_active, set_json_output


def run_cli(
    application: typer.Typer,
    *,
    args: list[str],
    prog_name: str | None,
    policy: CliErrorPolicy | None = None,
) -> None:
    json_output = json_errors_enabled(args)
    previous_json_output = json_output_active()
    if not _help_requested(args):
        set_json_output(json_output)
    try:
        application(args=args, prog_name=prog_name, standalone_mode=False)
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from None
    except (Exception, KeyboardInterrupt) as exc:
        if debug_errors_enabled(args):
            raise
        exit_code = render_exception(exc, json_output=json_output, policy=policy)
        raise SystemExit(exit_code) from None
    finally:
        set_json_output(previous_json_output)


def _help_requested(args: list[str]) -> bool:
    return any(arg in {"--help", "-h"} for arg in _root_arguments(args))


def _root_arguments(args: list[str]) -> list[str]:
    try:
        separator = args.index("--")
    except ValueError:
        return args
    return args[:separator]


__all__ = ["run_cli"]
