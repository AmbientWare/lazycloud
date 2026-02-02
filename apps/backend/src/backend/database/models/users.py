from models.users import SubscriptionState, UserRole, UserStatus
from pydantic import BaseModel

from backend.database.models.base import BaseDbModel


class User(BaseModel):
    """Pydantic model for a user account"""

    name: str
    email: str
    workos_id: str
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE
    subscription_state: SubscriptionState = SubscriptionState.WITHIN_LIMITS


class UserInDb(User, BaseDbModel):
    """Pydantic model for a user account that is stored in the database"""

    ...
