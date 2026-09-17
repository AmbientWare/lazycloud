"""Compact human summaries backed by complete machine payloads."""

from __future__ import annotations

import typer
from pydantic import JsonValue

from lazycloud.cli.components.cards import CardTone, notice_card, result_card
from lazycloud.cli.components.output import emit


def emit_result(
    ctx: typer.Context,
    *,
    payload: object,
    title: str | None = None,
    fields: dict[str, JsonValue],
    tone: CardTone = "neutral",
    message: str = "",
) -> None:
    """Show a concise human summary while keeping the complete JSON contract."""
    emit(
        ctx,
        payload=payload,
        view=result_card(fields, title=title, tone=tone, message=message),
    )


def emit_notice(
    ctx: typer.Context,
    *,
    payload: object,
    title: str | None = None,
    message: str,
    hint: str = "",
    tone: CardTone = "success",
) -> None:
    """Show one finite outcome without repeating payload bookkeeping."""
    emit(
        ctx,
        payload=payload,
        view=notice_card(message, title=title, hint=hint, tone=tone),
    )


__all__ = ["emit_notice", "emit_result"]
