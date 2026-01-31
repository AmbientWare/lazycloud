from datetime import datetime

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)


class ApiKey(BaseDbModel):
    """Pydantic model for a api key"""

    name: str
    user_id: UUIDStr
    value: str
    expires_at: datetime
