from models.api_keys import ApiKeyExpirationDays
from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str
    workos_id: str
    expires_at: ApiKeyExpirationDays


class UpdateApiKeyRequest(BaseModel):
    workos_id: str
    expires_at: ApiKeyExpirationDays
