"""Safe, semantic formatting for terminal-facing values."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from enum import Enum

from pydantic import JsonValue
from rich.console import Group, RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text
from shared.serialization import to_json_value

from lazycloud.cli.components import theme


def label(value: str) -> str:
    return value.replace("_", " ").strip().capitalize()


def text(item: object, *, style: Style | None = None) -> Text:
    rendered = Text(_scalar(item))
    if style is not None:
        rendered.stylize(style)
    return rendered


def status(value: object) -> Text:
    raw = value.value if isinstance(value, Enum) else value
    rendered = Text(str(raw).replace("_", " "))
    rendered.stylize(theme.state_style(raw))
    return rendered


def value(item: JsonValue, *, key: str = "") -> RenderableType:
    if isinstance(item, dict):
        return fields(item)
    if isinstance(item, list | tuple):
        if not item:
            return text(None)
        if all(not isinstance(member, dict | list | tuple) for member in item):
            return Text(", ").join(text(member) for member in item)
        return Group(*(value(member) for member in item))
    if key.lower() in {"state", "status", "phase", "lifecycle_state"}:
        return status(item)
    return text(item)


def fields(items: dict[str, JsonValue]) -> Table:
    grid = Table.grid(padding=(0, 2), expand=False)
    grid.add_column(style=theme.MUTED, no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    for key, item in _flatten_fields(items):
        grid.add_row(label(key).replace(".", " · "), value(item, key=key.rsplit(".", 1)[-1]))
    return grid


def cell(item: object, *, key: str = "") -> RenderableType:
    source = item.value if isinstance(item, Enum) else item
    return value(to_json_value(source), key=key)


def bytes_count(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1000
    return f"{int(size)} B"


def timestamp(value: datetime) -> str:
    localized = value.astimezone()
    return localized.strftime("%Y-%m-%d %H:%M:%S %Z")


def duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, remaining_seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _scalar(value: object) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return timestamp(value)
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _flatten_fields(
    items: dict[str, JsonValue],
    *,
    prefix: str = "",
) -> Iterator[tuple[str, JsonValue]]:
    for key, item in items.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            yield from _flatten_fields(item, prefix=path)
            continue
        if isinstance(item, list) and any(isinstance(member, dict | list) for member in item):
            for index, member in enumerate(item, start=1):
                indexed_path = f"{path}.{index}"
                if isinstance(member, dict):
                    yield from _flatten_fields(member, prefix=indexed_path)
                else:
                    yield indexed_path, member
            continue
        yield path, item


__all__ = [
    "bytes_count",
    "cell",
    "duration",
    "fields",
    "label",
    "status",
    "text",
    "timestamp",
    "value",
]
