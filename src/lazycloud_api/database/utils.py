import secrets
from datetime import datetime, timedelta, timezone

from lazycloud_api.database.api_keys import ApiKeyExpirationDays, ApiKeyExpirationMinutes


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

    raise ValueError(f"Invalid expiration type: {type(expiration)}")


def api_key_is_expired(expires_at: datetime) -> bool:
    """Check if the api key is expired"""
    # Ensure expires_at has timezone information
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < datetime.now(timezone.utc):
        return True

    return False
