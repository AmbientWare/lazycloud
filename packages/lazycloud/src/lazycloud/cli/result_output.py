"""Show and save function results without loading stored Python objects."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue
from rich.console import Group, RenderableType
from rich.highlighter import ReprHighlighter
from rich.text import Text
from shared.deployments import StubKind
from shared.function_display import build_function_result_display
from shared.function_payloads import (
    FunctionCloudpickleResult,
    FunctionJsonResult,
    FunctionPayloadEncoding,
    FunctionResultDisplay,
    FunctionResultDisplayKind,
    FunctionResultPayload,
    FunctionResultRichDisplay,
)
from shared.http.tasks import TaskResponse
from shared.serialization import to_json_value

from lazycloud._terminal import theme
from lazycloud._terminal.cards import result_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import json_default
from lazycloud.function_results import FunctionResultDecodeError, parse_function_result
from lazycloud.values import cloudpickle_bytes

_HIGHLIGHT = ReprHighlighter()
_RICH_EXTENSIONS = {
    FunctionResultDisplayKind.Image: ".png",
    FunctionResultDisplayKind.Html: ".html",
}


def task_result_view(task: TaskResponse) -> RenderableType:
    payload = _task_result_payload(task)
    if payload is None:
        return result_card(json_default(task.result), tone="success")
    if payload.encoding is FunctionPayloadEncoding.Json:
        return result_card(payload.value, tone="success")
    if payload.display is None:
        return result_card(
            {
                "encoding": FunctionPayloadEncoding.Cloudpickle.value,
                "size_bytes": payload.size_bytes,
                "message": "Opaque Python result; load it through a trusted Function handle.",
            },
            tone="success",
        )
    parts: list[RenderableType] = [display_text(payload.display)]
    if payload.display.rich is not None:
        parts.append(rich_display_hint(payload.display.rich))
    return Group(*parts)


def task_result_export(task: TaskResponse) -> ResultExport:
    payload = _task_result_payload(task)
    if payload is None:
        return ResultExport.from_payload(FunctionJsonResult(value=task.result))
    return ResultExport.from_payload(payload)


def _task_result_payload(task: TaskResponse) -> FunctionResultPayload | None:
    workload = task.workload
    if task.result is None or workload is None or workload.kind is not StubKind.Function:
        return None
    try:
        return parse_function_result(task.result)
    except FunctionResultDecodeError:
        return None


def display_text(display: FunctionResultDisplay) -> Text:
    return _HIGHLIGHT(Text(display.text))


def rich_display_hint(rich: FunctionResultRichDisplay) -> Text:
    extension = _RICH_EXTENSIONS[rich.kind]
    what = "an image" if rich.kind is FunctionResultDisplayKind.Image else "HTML"
    return Text(
        f"Also renders as {what}. Save it with --output FILE{extension}.",
        style=theme.MUTED,
    )


class ResultExport:
    """Write one result to a file; the extension picks the form."""

    def __init__(
        self,
        *,
        value: object = None,
        payload: FunctionResultPayload | None = None,
    ) -> None:
        self._value = value
        self._payload = payload
        self._display: FunctionResultDisplay | None = None
        self._display_built = False

    @classmethod
    def from_value(cls, value: object) -> ResultExport:
        return cls(value=value)

    @classmethod
    def from_payload(cls, payload: FunctionResultPayload) -> ResultExport:
        return cls(payload=payload)

    def display(self) -> FunctionResultDisplay | None:
        if not self._display_built:
            self._display = self._build_display()
            self._display_built = True
        return self._display

    def _build_display(self) -> FunctionResultDisplay | None:
        if isinstance(self._payload, FunctionCloudpickleResult):
            return self._payload.display
        if isinstance(self._payload, FunctionJsonResult):
            return FunctionResultDisplay(text=_json_text(self._payload.value))
        return build_function_result_display(self._value)

    def write(self, path: Path) -> None:
        extension = path.suffix.lower()
        if extension == ".png":
            path.write_bytes(self._rich_bytes(FunctionResultDisplayKind.Image, extension))
        elif extension == ".html":
            path.write_bytes(self._rich_bytes(FunctionResultDisplayKind.Html, extension))
        elif extension == ".txt":
            display = self.display()
            if display is None:
                raise _unavailable(extension, "this result has no text rendering")
            path.write_text(display.text + "\n", encoding="utf-8")
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

    def _rich_bytes(self, kind: FunctionResultDisplayKind, extension: str) -> bytes:
        display = self.display()
        rich = display.rich if display is not None else None
        if rich is None or rich.kind is not kind:
            what = "an image" if kind is FunctionResultDisplayKind.Image else "HTML"
            raise _unavailable(extension, f"this result does not render as {what}")
        if rich.kind is FunctionResultDisplayKind.Html:
            return rich.html.encode("utf-8")
        return rich.bytes_value()

    def _json_text(self) -> str:
        if isinstance(self._payload, FunctionJsonResult):
            return _json_text(self._payload.value)
        if self._payload is not None:
            raise _unavailable(".json", "this is a Python result; save it as .pkl or .txt")
        try:
            return _json_text(to_json_value(self._value))
        except (TypeError, ValueError) as exc:
            raise _unavailable(".json", "this result is not JSON-compatible") from exc

    def _pickled(self) -> bytes:
        if isinstance(self._payload, FunctionCloudpickleResult):
            return self._payload.bytes_value()
        if self._payload is not None:
            raise _unavailable(".pkl", "this is a JSON result; save it as .json")
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
    "display_text",
    "rich_display_hint",
    "task_result_export",
    "task_result_view",
]
