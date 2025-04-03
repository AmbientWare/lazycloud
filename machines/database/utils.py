import secrets
from datetime import datetime, timedelta, timezone

from machines.database.api_keys import ApiKeyExpirationDays, ApiKeyExpirationMinutes


def generate_api_key():
    prefix = "sk_"
    return prefix + secrets.token_hex(32)


def generate_api_key_expires_at(
    expiration: ApiKeyExpirationMinutes | ApiKeyExpirationDays,
):
    if isinstance(expiration, ApiKeyExpirationMinutes):
        return datetime.now(timezone.utc) + timedelta(minutes=expiration.value)

    elif isinstance(expiration, ApiKeyExpirationDays):
        return datetime.now(timezone.utc) + timedelta(days=expiration.value)

    raise ValueError(f"Invalid expiration type: {type(expiration)}")


def api_key_is_expired(expires_at: datetime) -> bool:
    if expires_at < datetime.now(timezone.utc):
        return True

    return False
