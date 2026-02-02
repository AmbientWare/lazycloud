from enum import StrEnum


class UserStatus(StrEnum):
    """Status of usage"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class SubscriptionState(StrEnum):
    """Subscription/billing state"""

    WITHIN_LIMITS = "within_limits"
    OVER_LIMITS = "over_limits"
    PAYMENT_FAILED = "payment_failed"
    TRIAL_EXPIRED = "trial_expired"
    SUSPENDED = "suspended"


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"
