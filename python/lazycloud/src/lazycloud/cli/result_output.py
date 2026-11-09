"""Show and save function results."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue
from rich.text import Text

from lazycloud._shared.function_display import (
    FunctionResultDisplay,
    build_function_result_display,
)
from lazycloud._shared.serialization import to_json_value
from lazycloud._terminal import theme
from lazycloud.cli.components.errors import ClientError
from lazycloud.values import cloudpickle_bytes


def rich_display_hint(display: FunctionResultDisplay) -> Text | None:
    """Where to save the image or HTML a result also renders as, if it renders as one."""
    if display.png is not None:
        what, extension = "an image", ".png"
    elif display.html is not None:
        what, extension = "HTML", ".html"
    else:
        return None
    return Text(
        f"Also renders as {what}. Save it with --output FILE{extension}.",
        style=theme.MUTED,
    )


class ResultExport:
    """Write one result to a file; the extension picks the form."""

    def __init__(self, *, value: object = None) -> None:
        self._value = value
        self._display: FunctionResultDisplay | None = None

    @classmethod
    def from_value(cls, value: object) -> ResultExport:
        return cls(value=value)

    def display(self) -> FunctionResultDisplay:
        if self._display is None:
            self._display = build_function_result_display(self._value)
        return self._display

    def write(self, path: Path) -> None:
        extension = path.suffix.lower()
        if extension == ".png":
            png = self.display().png
            if png is None:
                raise _unavailable(extension, "this result does not render as an image")
            path.write_bytes(png)
        elif extension == ".html":
            html = self.display().html
            if html is None:
                raise _unavailable(extension, "this result does not render as HTML")
            path.write_bytes(html.encode("utf-8"))
        elif extension == ".txt":
            path.write_text(self.display().text + "\n", encoding="utf-8")
        elif extension == ".json":
            path.write_text(self._json_text() + "\n", encoding="utf-8")
        elif extension in {".pkl", ".pickle"}:
            path.write_bytes(self._pickled())
        else:
            raise ClientError(
                f"cannot save a result as {path.name!r}",
                type="result_output_unsupported",
                title="Unsupported result file",
                hint="Use a .png, .html, .txt, .json, .pkl, or .pickle file name.",
            )

    def _json_text(self) -> str:
        try:
            return _json_text(to_json_value(self._value))
        except (TypeError, ValueError) as exc:
            raise _unavailable(".json", "this result is not JSON-compatible") from exc

    def _pickled(self) -> bytes:
        try:
            return cloudpickle_bytes(self._value)
        except Exception as exc:
            raise _unavailable(".pkl", "this result cannot be pickled") from exc


def _json_text(value: JsonValue) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _unavailable(extension: str, reason: str) -> ClientError:
    return ClientError(
        f"cannot save this result as {extension}: {reason}",
        type="result_output_unavailable",
        title="Result file unavailable",
    )


__all__ = [
    "ResultExport",
    "rich_display_hint",
]
