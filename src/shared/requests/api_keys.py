from pydantic import BaseModel

from lazycloud_api.database.api_keys import ApiKeyExpirationDays


class CreateApiKeyRequest(BaseModel):
    name: str
    user_id: str
    expires_at: ApiKeyExpirationDays


class UpdateApiKeyRequest(BaseModel):
    user_id: str
    expires_at: ApiKeyExpirationDays


class DeleteApiKeyRequest(BaseModel):
    api_key_id: int | None = None
    user_id: str | None = None
