"""Compact human summaries backed by complete machine payloads."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import JsonValue
from rich.pretty import Pretty
from rich.text import Text

from lazycloud.cli.components import theme
from lazycloud.cli.components.cards import CardTone, notice_card, result_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import (
    console,
    emit,
    error_console,
    json_default,
    json_output_enabled,
)
from lazycloud.cli.result_output import ResultExport, rich_display_hint


def emit_python_result(ctx: typer.Context, value: object, *, output: Path | None = None) -> None:
    export = ResultExport.from_value(value)
    if output is not None:
        export.write(output)
    if json_output_enabled(ctx):
        try:
            payload = json_default(value)
        except (TypeError, ValueError) as exc:
            raise ClientError(
                "The call completed, but its result cannot be represented as JSON.",
                type="result_not_json_serializable",
                title="Result requires Python output",
                hint="Omit --json to display the result, or use the Python SDK to retain its type.",
            ) from exc
        emit(ctx, payload=payload, view="")
        return
    console.print()
    if isinstance(value, str):
        console.print(value, markup=False, highlight=False)
    else:
        console.print(Pretty(value))
    if output is not None:
        error_console.print(Text(f"Saved {output}", style=theme.MUTED))
        return
    display = export.display()
    if display is not None and display.rich is not None:
        error_console.print(rich_display_hint(display.rich))


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


__all__ = ["emit_notice", "emit_python_result", "emit_result"]
