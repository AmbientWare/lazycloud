from pydantic import BaseModel

from lazycloud_api.database.api_keys import ApiKeyExpirationDays


class CreateApiKeyRequest(BaseModel):
    name: str
    clerk_id: str
    expires_at: ApiKeyExpirationDays


class UpdateApiKeyRequest(BaseModel):
    clerk_id: str
    expires_at: ApiKeyExpirationDays
