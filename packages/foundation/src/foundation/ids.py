from __future__ import annotations

from uuid import UUID


def try_uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def optional_uuid(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = try_uuid(value)
    if normalized is None:
        msg = f"{field} must be a UUID, got {value!r}"
        raise ValueError(msg)
    return normalized


def required_uuid(value: str | None, *, field: str) -> str:
    if value is None:
        msg = f"{field} is required"
        raise ValueError(msg)
    normalized = optional_uuid(value, field=field)
    if normalized is None:
        msg = f"{field} is required"
        raise ValueError(msg)
    return normalized
