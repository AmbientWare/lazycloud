import json
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from models.api_keys import ApiKeyExpirationDays, ApiKeyExpirationMinutes

from backend.config import app_config

FERNET = Fernet(app_config.DB_SECRET_KEY.encode())


def encrypt_string(value: str) -> str:
    encrypted_bytes = FERNET.encrypt(value.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def decrypt_string(encrypted_str: str) -> str:
    decrypted_bytes = FERNET.decrypt(encrypted_str.encode("utf-8"))
    return decrypted_bytes.decode("utf-8")


def encrypt_dict(secrets_dict: dict[str, str]) -> str:
    return encrypt_string(json.dumps(secrets_dict))


def decrypt_dict(encrypted_str: str) -> dict[str, str]:
    decrypted_str = decrypt_string(encrypted_str)
    return json.loads(decrypted_str)


def generate_api_key():
    prefix = "sk_"
    return prefix + secrets.token_hex(32)


def generate_api_key_expires_at(
    expiration: ApiKeyExpirationMinutes | ApiKeyExpirationDays,
):
    if isinstance(expiration, ApiKeyExpirationMinutes):
        if expiration.value == ApiKeyExpirationMinutes.NEVER:
            return datetime.max.replace(tzinfo=timezone.utc)

        return datetime.now(timezone.utc) + timedelta(minutes=expiration.value)

    elif isinstance(expiration, ApiKeyExpirationDays):
        if expiration.value == ApiKeyExpirationDays.NEVER:
            return datetime.max.replace(tzinfo=timezone.utc)

        return datetime.now(timezone.utc) + timedelta(days=expiration.value)


def api_key_is_expired(expires_at: datetime) -> bool:
    """Check if the api key is expired"""
    # Ensure expires_at has timezone information
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < datetime.now(timezone.utc):
        return True

    return False


def validate_workspace_name(name: str) -> str:
    """Validate and normalize a workspace name"""
    # Strip whitespace
    name = name.strip()

    # Check length
    if not name:
        raise ValueError("Workspace name cannot be empty")
    if len(name) > 100:
        raise ValueError("Workspace name must be 100 characters or less")

    # Check that it contains at least one alphanumeric character
    if not any(c.isalnum() for c in name):
        raise ValueError("Workspace name must contain at least one letter or number")

    # Prevent excessive consecutive spaces
    if "  " in name:
        raise ValueError("Workspace name cannot contain consecutive spaces")

    # Check for allowed characters using built-in methods
    allowed_chars = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_'()."
    )
    if not all(c in allowed_chars for c in name):
        raise ValueError(
            "Workspace name can only contain letters, numbers, spaces, and basic punctuation (- _ ' . ( ))"
        )

    return name
