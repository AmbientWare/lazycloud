from models.api_keys import ApiKeyExpirationDays
from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str
    clerk_id: str
    expires_at: ApiKeyExpirationDays


class UpdateApiKeyRequest(BaseModel):
    clerk_id: str
    expires_at: ApiKeyExpirationDays
