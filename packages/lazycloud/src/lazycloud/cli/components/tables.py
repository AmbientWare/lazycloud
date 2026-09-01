"""Responsive, safe collection views for terminal output."""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.console import RenderableType
from rich.table import Table

from lazycloud.cli.components import formatting, theme
from lazycloud.cli.components.cards import empty_state


def resource_table(
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    empty: str | None = None,
) -> RenderableType:
    if not rows and empty:
        return empty_state(title, empty)
    output = Table(
        title=title,
        title_justify="left",
        title_style=theme.EMPHASIS,
        box=box.SIMPLE_HEAD,
        header_style=theme.TABLE_HEADER,
        row_styles=(theme.PLAIN, theme.ROW_ALT),
        collapse_padding=True,
        pad_edge=False,
        expand=True,
    )
    normalized_columns = [str(column) for column in columns]
    for column in normalized_columns:
        output.add_column(formatting.label(column), overflow="fold")
    for row in rows:
        output.add_row(
            *(
                formatting.cell(item, key=normalized_columns[index])
                for index, item in enumerate(row)
            )
        )
    return output


__all__ = ["resource_table"]
