from pydantic import BaseModel

from lazycloud_api.database.api_keys import ApiKeyRole


class UserData(BaseModel):
    user_id: str
    role: ApiKeyRole
