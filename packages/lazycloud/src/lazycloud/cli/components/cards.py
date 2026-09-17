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
    payload: JsonValue,
    *,
    title: str | None = None,
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
    message: str,
    *,
    title: str | None = None,
    hint: str = "",
    tone: CardTone = "info",
) -> RenderableType:
    body = Text(message, style=theme.PLAIN if tone == "neutral" else _tone_style(tone))
    if hint:
        body.append("\nNext step  ", style=theme.MUTED)
        body.append(hint)
    return card(title, body, tone=tone)


def card(
    title: str | None,
    body: RenderableType,
    *,
    tone: CardTone = "neutral",
) -> RenderableType:
    if title is None:
        return body
    panel = Panel(
        body,
        title=Text(title, style=theme.EMPHASIS),
        title_align="left",
        border_style=_tone_style(tone),
        padding=(0, 1),
        expand=False,
    )
    return Constrain(panel, width=CARD_MAX_WIDTH)


def empty_state(message: str) -> RenderableType:
    return formatting.text(message, style=theme.MUTED)


def _sequence(items: list[JsonValue]) -> RenderableType:
    if not items:
        return formatting.text(None)
    parts: list[RenderableType] = []
    for item in items:
        if isinstance(item, dict):
            if parts:
                parts.append(Text())
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
