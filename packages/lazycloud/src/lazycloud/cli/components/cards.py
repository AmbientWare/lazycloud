"""Composable terminal cards for finite command results."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from lazycloud.cli.components import formatting, theme

CardTone = Literal["neutral", "info", "success", "warning"]


def result_card(
    title: str,
    payload: JsonValue,
    *,
    tone: CardTone = "neutral",
    message: str = "",
) -> Panel:
    parts: list[RenderableType] = []
    if message:
        parts.append(formatting.text(message))
    if isinstance(payload, dict):
        parts.append(formatting.fields(payload))
    elif isinstance(payload, list):
        parts.append(_sequence(payload))
    elif payload is not None:
        parts.append(formatting.value(payload))
    if not parts:
        parts.append(formatting.text("Done", style=theme.SUCCESS))
    return Panel(
        Group(*parts),
        title=_title(title, tone),
        title_align="left",
        border_style=_tone_style(tone),
        padding=(1, 2),
    )


def notice_card(
    title: str,
    message: str,
    *,
    hint: str = "",
    tone: CardTone = "info",
) -> Panel:
    body = Text(message)
    if hint:
        body.append("\n\nNext step  ", style=theme.MUTED)
        body.append(hint)
    return Panel(
        body,
        title=_title(title, tone),
        title_align="left",
        border_style=_tone_style(tone),
        padding=(1, 2),
    )


def empty_state(title: str, message: str) -> Panel:
    return notice_card(title, message, tone="neutral")


def _sequence(items: list[JsonValue]) -> RenderableType:
    if not items:
        return formatting.text(None)
    lines: list[Text] = []
    for item in items:
        line = Text("• ", style=theme.MUTED)
        if isinstance(item, dict):
            summary = ", ".join(
                f"{formatting.label(str(key))}: {value}" for key, value in item.items()
            )
            line.append(summary)
        else:
            line.append(str(item))
        lines.append(line)
    return Group(*lines)


def _title(title: str, tone: CardTone) -> Text:
    prefix = {
        "neutral": "",
        "info": "Info · ",
        "success": "Done · ",
        "warning": "Warning · ",
    }[tone]
    return Text(f"{prefix}{title}", style=theme.EMPHASIS)


def _tone_style(tone: CardTone) -> Style:
    return {
        "neutral": theme.BORDER,
        "info": theme.INFO,
        "success": theme.SUCCESS,
        "warning": theme.WARNING,
    }[tone]


__all__ = ["CardTone", "empty_state", "notice_card", "result_card"]
