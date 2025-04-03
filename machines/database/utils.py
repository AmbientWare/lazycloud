import secrets
from datetime import datetime, timedelta, timezone

from machines.database.tokens import TokenExpirationMinutes, TokenExpirationDays


def generate_token():
    prefix = "sk_"
    return prefix + secrets.token_hex(32)


def generate_token_expires_at(expiration: TokenExpirationMinutes | TokenExpirationDays):
    if isinstance(expiration, TokenExpirationMinutes):
        return datetime.now(timezone.utc) + timedelta(minutes=expiration.value)

    elif isinstance(expiration, TokenExpirationDays):
        return datetime.now(timezone.utc) + timedelta(days=expiration.value)

    raise ValueError(f"Invalid expiration type: {type(expiration)}")


def token_is_expired(expires_at: datetime) -> bool:
    if expires_at < datetime.now(timezone.utc):
        return True

    return False
