from enum import StrEnum

from backend.database.models.base import BaseDbPydanticModel


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


class UserPydantic(BaseDbPydanticModel):
    """Pydantic model for a user account"""

    name: str
    email: str
    workos_id: str
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE
    subscription_state: SubscriptionState = SubscriptionState.WITHIN_LIMITS
