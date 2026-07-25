from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Protocol, TypeGuard

from pydantic import BaseModel, JsonValue, Secret, SecretBytes, SecretStr

_SECRET_TYPES = (Secret, SecretBytes, SecretStr)


class _DataclassInstance(Protocol):
    __dataclass_fields__: ClassVar[dict[str, dataclasses.Field[object]]]


def to_json_value(value: object) -> JsonValue:
    """Convert one declared JSON-compatible value without coercing unsafe shapes."""

    return _to_json_value(value, active=set())


def _to_json_value(value: object, *, active: set[int]) -> JsonValue:
    if isinstance(value, _SECRET_TYPES):
        raise ValueError("secret-bearing values are not JSON serializable")
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not JSON serializable")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, AbstractSet):
        raise ValueError("set values are not JSON serializable")
    if isinstance(value, BaseModel):
        _validate_pydantic_source(value, active=set())
        serialized: object = value.model_dump(mode="json", round_trip=True)
        return _to_json_value(serialized, active=active)
    if _is_dataclass_instance(value):
        return _convert_dataclass(value, active=active)
    if _is_list(value):
        return _convert_sequence(value, active=active)
    if _is_tuple(value):
        return _convert_sequence(value, active=active)
    if _is_mapping(value):
        return _convert_mapping(value, active=active)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _validate_source_structure(value: object, *, active: set[int]) -> None:
    """Reject shapes that Pydantic's JSON mode would otherwise mask or coerce."""

    if isinstance(value, _SECRET_TYPES):
        raise ValueError("secret-bearing values are not JSON serializable")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not JSON serializable")
    if isinstance(value, AbstractSet):
        raise ValueError("set values are not JSON serializable")
    if isinstance(value, BaseModel):
        _validate_pydantic_source(value, active=active)
        return
    if _is_dataclass_instance(value):
        identity = _enter_container(value, active)
        try:
            for field in dataclasses.fields(value):
                field_value: object = getattr(value, field.name)
                _validate_source_structure(field_value, active=active)
        finally:
            active.remove(identity)
        return
    if _is_list(value) or _is_tuple(value):
        identity = _enter_container(value, active)
        try:
            for item in value:
                _validate_source_structure(item, active=active)
        finally:
            active.remove(identity)
        return
    if _is_mapping(value):
        identity = _enter_container(value, active)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                _validate_source_structure(item, active=active)
        finally:
            active.remove(identity)


def _validate_pydantic_source(value: BaseModel, *, active: set[int]) -> None:
    identity = _enter_container(value, active)
    try:
        for field_name in type(value).model_fields:
            if field_name not in value.__dict__:
                continue
            field_value: object = value.__dict__[field_name]
            _validate_source_structure(field_value, active=active)
        extra: object = value.__pydantic_extra__
        if extra is not None:
            _validate_source_structure(extra, active=active)
        for field_name in type(value).model_computed_fields:
            computed_value: object = getattr(value, field_name)
            _validate_source_structure(computed_value, active=active)
    finally:
        active.remove(identity)


def _convert_dataclass(
    value: _DataclassInstance,
    *,
    active: set[int],
) -> dict[str, JsonValue]:
    identity = _enter_container(value, active)
    try:
        converted: dict[str, JsonValue] = {}
        for field in dataclasses.fields(value):
            field_value: object = getattr(value, field.name)
            converted[field.name] = _to_json_value(field_value, active=active)
        return converted
    finally:
        active.remove(identity)


def _convert_sequence(
    value: list[object] | tuple[object, ...],
    *,
    active: set[int],
) -> list[JsonValue]:
    identity = _enter_container(value, active)
    try:
        return [_to_json_value(item, active=active) for item in value]
    finally:
        active.remove(identity)


def _convert_mapping(
    value: Mapping[object, object],
    *,
    active: set[int],
) -> dict[str, JsonValue]:
    identity = _enter_container(value, active)
    try:
        converted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            converted[key] = _to_json_value(item, active=active)
        return converted
    finally:
        active.remove(identity)


def _enter_container(value: object, active: set[int]) -> int:
    identity = id(value)
    if identity in active:
        raise ValueError("recursive values are not JSON serializable")
    active.add(identity)
    return identity


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_dataclass_instance(value: object) -> TypeGuard[_DataclassInstance]:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


__all__ = ["to_json_value"]
