import secrets
from datetime import datetime, timedelta, timezone

from machines.database.tokens import TokenExpiration


def generate_token():
    prefix = "sk_"
    return prefix + secrets.token_hex(32)


def generate_token_expires_at(expiration: TokenExpiration):
    return datetime.now(timezone.utc) + timedelta(days=expiration.value)


def token_is_expired(expires_at: datetime) -> bool:
    if expires_at < datetime.now(timezone.utc):
        return True

    return False
