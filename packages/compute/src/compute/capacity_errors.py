from shared.errors import ConflictError, UpstreamUnavailableError


class ProviderAuthorizationPendingError(UpstreamUnavailableError):
    """Provider authorization validation has not finished."""


class CapacityReservationConflictError(ConflictError):
    """Capacity owner state changed while an operation was in flight."""


class CapacityReservationLockContendedError(CapacityReservationConflictError):
    """Another operation owns the capacity-owner lease."""


class CapacityReservationLeaseLostError(CapacityReservationConflictError):
    """The capacity-owner lease could not be renewed or was replaced."""
