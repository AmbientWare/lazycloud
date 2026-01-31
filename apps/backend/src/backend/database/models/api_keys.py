from datetime import datetime

from backend.database.models.base import (
    BaseDbPydanticModel,
    UUIDStr,
)


class ApiKeyPydantic(BaseDbPydanticModel):
    """Pydantic model for a api key"""

    name: str
    user_id: UUIDStr
    value: str
    expires_at: datetime
