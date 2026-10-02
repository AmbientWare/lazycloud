from __future__ import annotations

import re

_CODE_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def domain_error_code(error_type: type[DomainError]) -> str:
    """The wire code an error type maps to, for callers that match on it."""
    name = error_type.__name__.removesuffix("Error")
    return _CODE_BOUNDARY.sub("_", name).lower()


class DomainError(Exception):
    """Base error for domain failures that map to client-visible HTTP errors."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code or domain_error_code(type(self))


class InvalidInputError(DomainError):
    """Request is well-formed but semantically invalid."""


__all__ = [
    "DomainError",
    "InvalidInputError",
    "domain_error_code",
]
