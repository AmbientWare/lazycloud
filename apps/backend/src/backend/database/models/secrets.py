from typing import Any

from cryptography.fernet import InvalidToken
from models.secrets import SecretSource, SecretState
from pydantic import field_serializer, field_validator

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)
from backend.database.utils import decrypt_string, encrypt_string


class Secret(BaseDbModel):
    """Pydantic model for deployment secrets with automatic encryption/decryption."""

    deployment_id: UUIDStr
    key: str
    value: str
    source: SecretSource
    state: SecretState

    @field_validator("value", mode="before")
    @classmethod
    def decrypt_value(cls, value: Any) -> str:
        """Automatically decrypt value when loading from database."""
        if isinstance(value, str):
            try:
                return decrypt_string(value)
            except InvalidToken:
                # Not encrypted (migration or new secret creation)
                return value
            except Exception as e:
                # Real decryption error
                raise ValueError(f"Failed to decrypt secret value: {e}") from e
        return value

    @field_serializer("value", when_used="always")
    def serialize_value(self, value: str) -> str:
        """Automatically encrypt value when dumping for database storage."""
        # Always encrypt when serializing for storage
        try:
            return encrypt_string(value)
        except Exception as e:
            raise ValueError(f"Failed to encrypt secret value: {e}") from e
