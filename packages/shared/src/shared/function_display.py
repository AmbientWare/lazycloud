"""Describe a Python result for surfaces that never load it.

Rendering is the object's decision: `repr` for text, `_repr_png_` or
`_repr_html_` when it defines them. No type is named here.
"""

from __future__ import annotations

import logging
import pprint
from typing import TypeGuard

from shared.function_payloads import (
    FUNCTION_RESULT_DISPLAY_HTML_MAX_CHARS,
    FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES,
    FUNCTION_RESULT_DISPLAY_TEXT_MAX_CHARS,
    FunctionResultDisplay,
    FunctionResultHtmlDisplay,
    FunctionResultImageDisplay,
    FunctionResultRichDisplay,
)

LOGGER = logging.getLogger(__name__)
_TEXT_WIDTH = 88
_TRUNCATION_MARK = "..."


def build_function_result_display(value: object) -> FunctionResultDisplay:
    return FunctionResultDisplay(text=_display_text(value), rich=_rich_display(value))


def _display_text(value: object) -> str:
    try:
        text = pprint.pformat(value, width=_TEXT_WIDTH, compact=True, sort_dicts=False)
    except Exception as exc:
        text = f"<{type(value).__name__} repr failed: {type(exc).__name__}>"
    if len(text) > FUNCTION_RESULT_DISPLAY_TEXT_MAX_CHARS:
        keep = FUNCTION_RESULT_DISPLAY_TEXT_MAX_CHARS - len(_TRUNCATION_MARK)
        text = text[:keep] + _TRUNCATION_MARK
    return text


def _rich_display(value: object) -> FunctionResultRichDisplay | None:
    if isinstance(value, type):
        return None
    png = _call_repr_method(value, "_repr_png_")
    if isinstance(png, bytes) and 0 < len(png) <= FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES:
        return FunctionResultImageDisplay.from_bytes(png)
    html = _call_repr_method(value, "_repr_html_")
    if isinstance(html, str) and 0 < len(html) <= FUNCTION_RESULT_DISPLAY_HTML_MAX_CHARS:
        return FunctionResultHtmlDisplay(html=html)
    return None


def _call_repr_method(value: object, name: str) -> object:
    method = getattr(value, name, None)
    if not callable(method):
        return None
    try:
        result: object = method()
    except Exception:
        LOGGER.debug("%s.%s failed", type(value).__name__, name, exc_info=True)
        return None
    # `_repr_*_` may return `(data, metadata)`.
    if _is_tuple(result):
        return result[0] if result else None
    return result


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


__all__ = ["build_function_result_display"]
