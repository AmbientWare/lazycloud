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


class NotFoundError(DomainError):
    """Requested resource does not exist or is not visible to the caller."""


class ConflictError(DomainError):
    """Request conflicts with the current state of a resource."""


class ExpiredCursorError(ConflictError):
    """A realtime cursor predates the history still retained by its stream."""


class CapacityLimitReachedError(ConflictError):
    """The caller is at a configured capacity limit.

    A distinct type because the condition reaches the caller through several
    entry points, and it is a state they own and can act on — not a malformed
    request and not an upstream outage. The message names the limit and what is
    already held, so the answer to "how much" does not require another call.
    """


class EndpointReplicaLimitReachedError(CapacityLimitReachedError):
    """An endpoint already holds its configured maximum number of containers."""


class DiskVolumePendingError(ConflictError):
    """A disk's provider volume is still being created, attached or detached.

    The work continues without the caller and a repeat request picks it up, so
    callers should retry shortly rather than give up.
    """


class ContainerLifetimeEndedError(ConflictError):
    """The control plane recorded this container as finished.

    A worker still running it missed the stop. The worker should end the
    container rather than retry: the platform will not accept more usage for it.
    """


class InvalidInputError(DomainError):
    """Request is well-formed but semantically invalid."""


class UpstreamUnavailableError(DomainError):
    """A required backing service or worker is unavailable."""


class UpstreamTimeoutError(DomainError):
    """A required backing service or worker exceeded the request deadline."""


class PaymentRequiredError(DomainError):
    """This account owes money, so the platform will not start more work.

    The platform's own decision, taken before anything runs, and the caller acts
    on it by paying rather than by retrying.
    """


__all__ = [
    "CapacityLimitReachedError",
    "ConflictError",
    "ContainerLifetimeEndedError",
    "DiskVolumePendingError",
    "DomainError",
    "EndpointReplicaLimitReachedError",
    "ExpiredCursorError",
    "InvalidInputError",
    "NotFoundError",
    "PaymentRequiredError",
    "UpstreamTimeoutError",
    "UpstreamUnavailableError",
    "domain_error_code",
]
