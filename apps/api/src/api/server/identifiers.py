from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator
from shared.errors import InvalidInputError


def _validated_identifier(value: str | None) -> str | None:
    """Reject a malformed resource identifier at the request boundary.

    These filters address UUID columns. Without validation an arbitrary string
    reaches PostgreSQL and raises a DataError, which surfaces as an unhandled 500
    rather than a typed client error naming the bad input.
    """
    if value is None:
        return None
    try:
        UUID(value)
    except ValueError as exc:
        raise InvalidInputError(f"identifier is not a valid UUID: {value}") from exc
    return value


def resource_identifier(value: str, *, resource: str) -> str:
    """The same rule for a path segment, which must be present rather than optional.

    A path parameter reaching a uuid column unchecked raises a `DataError` deep in
    the driver, and an unhandled one of those is a 500 for what is a malformed
    request. Named by resource so the refusal says which identifier was wrong.
    """
    try:
        UUID(value)
    except ValueError as exc:
        raise InvalidInputError(f"{resource} identifier is not a valid UUID: {value}") from exc
    return value


type identifier_filter = Annotated[str | None, AfterValidator(_validated_identifier)]


__all__ = ["identifier_filter", "resource_identifier"]
