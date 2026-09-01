"""Composable terminal cards for finite command results."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue
from rich.console import Group, RenderableType
from rich.constrain import Constrain
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from lazycloud.cli.components import formatting, theme

CardTone = Literal["neutral", "info", "success", "warning", "error"]
CARD_MAX_WIDTH = 88


def result_card(
    title: str,
    payload: JsonValue,
    *,
    tone: CardTone = "neutral",
    message: str = "",
) -> RenderableType:
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
        parts.append(formatting.text("No result", style=theme.MUTED))
    return card(title, Group(*parts), tone=tone)


def notice_card(
    title: str,
    message: str,
    *,
    hint: str = "",
    tone: CardTone = "info",
) -> RenderableType:
    body = Text(message)
    if hint:
        body.append("\n\nNext step  ", style=theme.MUTED)
        body.append(hint)
    return card(title, body, tone=tone)


def card(
    title: str,
    body: RenderableType,
    *,
    tone: CardTone = "neutral",
) -> RenderableType:
    panel = Panel(
        body,
        title=Text(title, style=theme.EMPHASIS),
        title_align="left",
        border_style=_tone_style(tone),
        padding=(1, 2),
        expand=False,
    )
    return Constrain(panel, width=CARD_MAX_WIDTH)


def empty_state(title: str, message: str) -> RenderableType:
    return notice_card(title, message, tone="neutral")


def _sequence(items: list[JsonValue]) -> RenderableType:
    if not items:
        return formatting.text(None)
    parts: list[RenderableType] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, dict):
            parts.append(Text(f"Result {index}", style=theme.MUTED + theme.EMPHASIS))
            parts.append(formatting.fields(item))
        else:
            line = Text("• ", style=theme.MUTED)
            line.append(str(item))
            parts.append(line)
    return Group(*parts)


def _tone_style(tone: CardTone) -> Style:
    return {
        "neutral": theme.BORDER,
        "info": theme.INFO,
        "success": theme.SUCCESS,
        "warning": theme.WARNING,
        "error": theme.ERROR,
    }[tone]


__all__ = [
    "CARD_MAX_WIDTH",
    "CardTone",
    "card",
    "empty_state",
    "notice_card",
    "result_card",
]
