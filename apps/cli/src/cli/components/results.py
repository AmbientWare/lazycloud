"""Compact human summaries backed by complete machine payloads."""

from __future__ import annotations

import typer
from lazycloud.cli.components.cards import CardTone, result_card
from lazycloud.cli.components.output import emit
from pydantic import JsonValue


def emit_result(
    ctx: typer.Context,
    *,
    payload: object,
    title: str,
    fields: dict[str, JsonValue],
    tone: CardTone = "neutral",
    message: str = "",
) -> None:
    """Show the operator summary while keeping the complete JSON contract."""
    emit(
        ctx,
        payload=payload,
        view=result_card(title, fields, tone=tone, message=message),
    )


__all__ = ["emit_result"]
