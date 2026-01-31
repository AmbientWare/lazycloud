from datetime import datetime

from pydantic import BaseModel

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)


class ApiKey(BaseModel):
    """Pydantic model for a api key"""

    name: str
    user_id: UUIDStr
    value: str
    expires_at: datetime


class ApiKeyInDb(ApiKey, BaseDbModel):
    """Pydantic model for a api key that is stored in the database"""

    ...
