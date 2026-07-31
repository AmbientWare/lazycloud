from __future__ import annotations

import re

_CODE_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _derived_code(error_type: type[DomainError]) -> str:
    name = error_type.__name__.removesuffix("Error")
    return _CODE_BOUNDARY.sub("_", name).lower()


class DomainError(Exception):
    """Base error for domain failures that map to client-visible HTTP errors."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code or _derived_code(type(self))


class NotFoundError(DomainError):
    """Requested resource does not exist or is not visible to the caller."""


class ConflictError(DomainError):
    """Request conflicts with the current state of a resource."""


class ExpiredCursorError(ConflictError):
    """A realtime cursor predates the history still retained by its stream."""


class InvalidInputError(DomainError):
    """Request is well-formed but semantically invalid."""


class UpstreamUnavailableError(DomainError):
    """A required backing service or worker is unavailable."""


__all__ = [
    "ConflictError",
    "DomainError",
    "ExpiredCursorError",
    "InvalidInputError",
    "NotFoundError",
    "UpstreamUnavailableError",
]
