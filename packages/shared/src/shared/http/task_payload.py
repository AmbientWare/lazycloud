from __future__ import annotations

from pydantic import Field, JsonValue, TypeAdapter

from shared.http.base import HttpModel

JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class HttpTaskPayload(HttpModel):
    args: list[JsonValue] | None = None
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    result_format: str = ""


def serialize_http_task_payload(
    body: bytes | str = b"",
    *,
    query_params: dict[str, list[str]] | None = None,
) -> HttpTaskPayload:
    payload = _decode_json_object(body)
    args = None
    kwargs: dict[str, JsonValue] = {}
    result_format = ""
    if payload:
        raw_result_format = payload.pop("result_format", "")
        if isinstance(raw_result_format, str):
            result_format = raw_result_format
        raw_args = payload.pop("args", None)
        if isinstance(raw_args, list):
            args = list(raw_args)
        raw_kwargs = payload.pop("kwargs", None)
        if isinstance(raw_kwargs, dict):
            kwargs = dict(raw_kwargs)
        elif payload:
            kwargs = dict(payload)
    if query_params:
        kwargs.update(_coerce_query_params(query_params))
    return HttpTaskPayload(args=args, kwargs=kwargs, result_format=result_format)


def _decode_json_object(body: bytes | str) -> dict[str, JsonValue]:
    if body in {b"", ""}:
        return {}
    raw = body.decode() if isinstance(body, bytes) else body
    try:
        payload = JSON_VALUE_ADAPTER.validate_json(raw)
    except ValueError as exc:
        msg = "invalid request payload"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "request payload must be a JSON object"
        raise ValueError(msg)
    return payload


def _coerce_query_params(query_params: dict[str, list[str]]) -> dict[str, JsonValue]:
    kwargs: dict[str, JsonValue] = {}
    for key, values in query_params.items():
        if len(values) == 1:
            kwargs[key] = _coerce_query_value(values[0])
            continue
        kwargs[key] = [_coerce_query_value(value) for value in values]
    return kwargs


def _coerce_query_value(value: str) -> JsonValue:
    try:
        return float(value)
    except ValueError:
        return value


__all__ = ["HttpTaskPayload", "serialize_http_task_payload"]
