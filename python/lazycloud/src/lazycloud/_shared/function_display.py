"""Describe a Python result for surfaces that never load it.

Rendering is the object's decision: `repr` for text, `_repr_png_` or
`_repr_html_` when it defines them. No type is named here.
"""

from __future__ import annotations

import logging
import pprint
from dataclasses import dataclass
from typing import TypeGuard

# The runner protocol's display limits.
TEXT_MAX_CHARS = 64 * 1024
HTML_MAX_CHARS = 256 * 1024
IMAGE_MAX_BYTES = 1024 * 1024

LOGGER = logging.getLogger(__name__)
_TEXT_WIDTH = 88
_TRUNCATION_MARK = "..."


@dataclass(frozen=True, slots=True)
class FunctionResultDisplay:
    """A result's text, plus a PNG or HTML rendering when it defines one."""

    text: str
    png: bytes | None = None
    html: str | None = None


def build_function_result_display(value: object) -> FunctionResultDisplay:
    text = _display_text(value)
    if isinstance(value, type):
        return FunctionResultDisplay(text=text)
    png = _call_repr_method(value, "_repr_png_")
    if isinstance(png, bytes) and 0 < len(png) <= IMAGE_MAX_BYTES:
        return FunctionResultDisplay(text=text, png=png)
    html = _call_repr_method(value, "_repr_html_")
    if isinstance(html, str) and 0 < len(html) <= HTML_MAX_CHARS:
        return FunctionResultDisplay(text=text, html=html)
    return FunctionResultDisplay(text=text)


def _display_text(value: object) -> str:
    try:
        text = pprint.pformat(value, width=_TEXT_WIDTH, compact=True, sort_dicts=False)
    except Exception as exc:
        text = f"<{type(value).__name__} repr failed: {type(exc).__name__}>"
    if len(text) > TEXT_MAX_CHARS:
        keep = TEXT_MAX_CHARS - len(_TRUNCATION_MARK)
        text = text[:keep] + _TRUNCATION_MARK
    return text


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


__all__ = ["FunctionResultDisplay", "build_function_result_display"]
