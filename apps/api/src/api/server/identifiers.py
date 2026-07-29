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


type identifier_filter = Annotated[str | None, AfterValidator(_validated_identifier)]


__all__ = ["identifier_filter"]
