from pydantic import BaseModel

from lazycloud_api.database.api_keys import ApiKeyExpirationDays


class JwtToken(BaseModel):
    sub: str
    expires_at: ApiKeyExpirationDays
