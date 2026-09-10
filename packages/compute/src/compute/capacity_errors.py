from shared.errors import ConflictError


class CapacityReservationConflictError(ConflictError):
    """Capacity owner state changed while an operation was in flight."""


class CapacityReservationLockContendedError(CapacityReservationConflictError):
    """Another operation owns the capacity-owner lease."""


class CapacityReservationLeaseLostError(CapacityReservationConflictError):
    """The capacity-owner lease could not be renewed or was replaced."""
