from models.api_keys import ApiKeyExpirationDays
from pydantic import BaseModel


class CreateApiKeyRequest(BaseModel):
    name: str
    expires_at: ApiKeyExpirationDays = ApiKeyExpirationDays.NEVER


class UpdateApiKeyRequest(BaseModel):
    expires_at: ApiKeyExpirationDays = ApiKeyExpirationDays.NEVER
