from enum import StrEnum

from pydantic import BaseModel


class SecretState(StrEnum):
    AWAITING_DEPLOYMENT = "awaiting_deployment"
    DEPLOYED = "deployed"


class SecretSource(StrEnum):
    COMPOSE = "compose"  # derived from definition in compose file
    USER = "user"  # added by user via UI/CLI


class BasicSecret(BaseModel):
    key: str
    value: str
    source: SecretSource
    state: SecretState | None = None


class SecretCollection(BaseModel):
    added: list[BasicSecret] | None = None
    removed: list[BasicSecret] | None = None
