"""Single output owner for the ``lazycloud`` and ``lazycloud-admin`` commands.

Decorative output uses the shared terminal consoles.
Machine-readable payloads go through ``print_payload``,
which in ``--json`` mode writes plain ``json.dumps`` to stdout. While JSON
output is active every decorative console write is routed to stderr, so stdout
stays a pure payload stream without per-command discipline.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Any

import typer
from pydantic import JsonValue
from rich.console import RenderableType
from shared.events import Event
from shared.serialization import to_json_value

from lazycloud._terminal.cards import empty_state, result_card
from lazycloud._terminal.streams import console, error_console
from lazycloud.cli.components.tables import resource_table
from lazycloud.json_contracts import parse_json_value


@dataclass(frozen=True, slots=True)
class CliContextState:
    json: bool = False
    debug: bool = False


def json_default(value: object) -> JsonValue:
    return to_json_value(value)


def parse_json_argument(raw: str) -> object:
    try:
        return parse_json_value(raw)
    except ValueError:
        return raw


def command_from_args(args: Sequence[str]) -> list[str]:
    if not args:
        msg = "command is required"
        raise typer.BadParameter(msg)
    return list(args)


def json_output_enabled(ctx: typer.Context) -> bool:
    return isinstance(ctx.obj, CliContextState) and ctx.obj.json


def print_json_line(payload: Any, *, file: IO[str]) -> None:
    """Write one plain JSON document: sorted keys, trailing newline, no Rich."""
    file.write(json.dumps(json_default(payload), sort_keys=True) + "\n")
    file.flush()


def emit(ctx: typer.Context, *, payload: Any, view: RenderableType) -> None:
    if json_output_enabled(ctx):
        print_json_line(payload, file=sys.stdout)
        return
    console.print(view)


def print_payload(
    ctx: typer.Context,
    payload: Any,
) -> None:
    normalized = json_default(payload)
    emit(
        ctx,
        payload=normalized,
        view=result_card(normalized),
    )


def write_stream(value: str, *, error: bool = False) -> None:
    """Write stream content exactly, without Rich markup or highlighting."""
    target = error_console if error else console
    target.file.write(value)
    target.file.flush()


def table(
    resource: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    title: str | None = None,
    empty: str | None = None,
) -> RenderableType:
    return resource_table(
        columns,
        rows,
        title=title,
        empty=empty or f"No {resource.lower()} found.",
    )


def event_table(events: Sequence[Event]) -> RenderableType:
    rows = [
        [
            item.created_at.isoformat(),
            item.level.value,
            item.action,
            item.resource_type,
            item.resource_id,
            item.message,
        ]
        for item in events
    ]
    return table(
        "events",
        ["time", "level", "action", "type", "resource", "message"],
        rows,
    )


def print_events_table(events: Sequence[Event]) -> None:
    if not events:
        console.print(empty_state("No events found."))
        return
    console.print(event_table(events))


__all__ = [
    "command_from_args",
    "emit",
    "event_table",
    "json_default",
    "json_output_enabled",
    "parse_json_argument",
    "print_events_table",
    "print_json_line",
    "print_payload",
    "table",
    "write_stream",
]
