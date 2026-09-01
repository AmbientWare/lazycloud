"""Single output owner for the ``lazycloud`` and ``lazycloud-admin`` commands.

Decorative output (tables, tips, progress lines) goes through ``console`` and
``error_console``. Machine-readable payloads go through ``print_payload``,
which in ``--json`` mode writes plain ``json.dumps`` to stdout. While JSON
output is active every decorative console write is routed to stderr, so stdout
stays a pure payload stream without per-command discipline.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from typing import IO, Any, Protocol, runtime_checkable

import typer
from pydantic import JsonValue
from rich.console import Console, RenderableType
from shared.events import Event
from shared.serialization import to_json_value

from lazycloud.cli.components.cards import CardTone, empty_state, result_card
from lazycloud.cli.components.tables import resource_table
from lazycloud.json_contracts import parse_json_value

_json_output_active = ContextVar("lazycloud_cli_json_output_active", default=False)


@dataclass(frozen=True, slots=True)
class CliContextState:
    json: bool = False
    debug: bool = False


def set_json_output(enabled: bool) -> None:
    """Declare whether stdout is reserved for JSON payloads this invocation.

    Called by the root command callback; while enabled, ``CliConsole`` routes
    decorative output to stderr.
    """
    _json_output_active.set(enabled)


def json_output_active() -> bool:
    return _json_output_active.get()


class CliConsole(Console):
    """Console for decorative CLI output.

    Resolves its target stream per write: stdout for human output, stderr for
    error output, and stderr for everything while JSON output is active so
    stray decorative prints can never corrupt a machine-readable payload.
    Rich drops styling automatically when the resolved stream is not a TTY.
    """

    @property
    def file(self) -> IO[str]:
        if self._file is not None:
            return self._file
        if self.stderr or json_output_active():
            return sys.stderr
        return sys.stdout

    @file.setter
    def file(self, new_file: IO[str]) -> None:
        self._file = new_file


console = CliConsole()
error_console = CliConsole(stderr=True)


@runtime_checkable
class ModelDumpable(Protocol):
    def model_dump(self, *, mode: str) -> object: ...


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
    *,
    title: str | None = None,
    tone: CardTone | None = None,
    message: str = "",
) -> None:
    normalized = json_default(payload)
    emit(
        ctx,
        payload=normalized,
        view=result_card(
            title or _command_title(ctx),
            normalized,
            tone=tone or _command_tone(ctx),
            message=message,
        ),
    )


def print_collection(
    ctx: typer.Context,
    payload: Any,
    *,
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    empty: str,
) -> None:
    emit(
        ctx,
        payload=payload,
        view=resource_table(title, columns, rows, empty=empty),
    )


def write_stream(value: str, *, error: bool = False) -> None:
    """Write stream content exactly, without Rich markup or highlighting."""
    target = error_console if error else console
    target.file.write(value)
    target.file.flush()


def payload_data(value: object) -> object:
    if isinstance(value, ModelDumpable):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if not field.name.startswith("_")
        }
    return value


def table(title: str, columns: list[str], rows: list[list[Any]]) -> RenderableType:
    return resource_table(title, columns, rows, empty="No items found.")


def _command_title(ctx: typer.Context) -> str:
    names = [part.replace("-", " ") for part in ctx.command_path.split()[1:]]
    if not names:
        return "Result"
    return " ".join(names).title()


def _command_tone(ctx: typer.Context) -> CardTone:
    command = (ctx.info_name or "").replace("-", "_")
    if command in {
        "activate",
        "add",
        "cancel",
        "checkpoint",
        "connect",
        "cp",
        "create",
        "create_app",
        "delete",
        "deploy",
        "disconnect",
        "download",
        "get",
        "modify",
        "move",
        "mv",
        "pause",
        "quickstart",
        "remove",
        "rename",
        "resume",
        "retry",
        "rm",
        "scale",
        "set",
        "start",
        "stop",
        "update",
    }:
        return "success"
    return "neutral"


def event_table(title: str, events: Sequence[Event]) -> RenderableType:
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
    return table(title, ["time", "level", "action", "type", "resource", "message"], rows)


def print_events_table(title: str, events: Sequence[Event]) -> None:
    if not events:
        console.print(empty_state(title, "No events found."))
        return
    console.print(event_table(title, events))


__all__ = [
    "CliConsole",
    "command_from_args",
    "console",
    "emit",
    "error_console",
    "event_table",
    "json_default",
    "json_output_active",
    "json_output_enabled",
    "parse_json_argument",
    "payload_data",
    "print_collection",
    "print_events_table",
    "print_json_line",
    "print_payload",
    "set_json_output",
    "table",
    "write_stream",
]
